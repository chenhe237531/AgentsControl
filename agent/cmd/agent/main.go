// afc-agent：设备端常驻进程。
//
// 进程模型（骨架）：
//   - heartbeat 循环：60s，独立 goroutine；发送失败只记日志，不阻塞其它通道
//   - flush 循环：30s 批量提交增量；先 Peek 再 Post，成功才 Ack（至少一次语义 + 服务端幂等）
//   - snapshot 循环：60min；唤醒/启动时立即补一次（W05 实现"先快照后增量"）
//
// 退出：收到 SIGINT/SIGTERM 后停止领取新批次，给在途请求 5s 收尾，然后退出。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/jackma3327/AgentsControl/agent/internal/buffer"
	"github.com/jackma3327/AgentsControl/agent/internal/collect"
	"github.com/jackma3327/AgentsControl/agent/internal/config"
	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
	"github.com/jackma3327/AgentsControl/agent/internal/transport"
)

var (
	version = "dev"
	commit  = "none"
)

func main() {
	if len(os.Args) > 1 && (os.Args[1] == "install" || os.Args[1] == "uninstall") {
		// 子命令分发：install / uninstall
		switch os.Args[1] {
		case "install":
			os.Exit(runInstall(os.Args[2:]))
		case "uninstall":
			os.Exit(runUninstall())
		}
	}

	showVersion := flag.Bool("version", false, "打印版本并退出")
	dryRun := flag.Bool("dry-run", false, "只打印待发批次，不联网")
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if *showVersion {
		slog.Info("afc-agent", "version", version, "commit", commit)
		return
	}

	cfg, err := config.Load()
	if err != nil {
		slog.Error("config invalid", "err", err)
		os.Exit(2) // 配置错误用独立退出码，便于服务管理器区分
	}

	buf, err := openBuffer(cfg)
	if err != nil {
		slog.Error("open buffer", "err", err)
		os.Exit(1)
	}
	client := transport.New(cfg.APIBaseURL, cfg.NodeID, cfg.Token)
	collector := collect.New(cfg.NodeID, cfg.AgentVer)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	slog.Info("agent started",
		"node", cfg.NodeID, "api", cfg.APIBaseURL, "version", cfg.AgentVer,
		"buffer", cfg.BufferKind,
		"heartbeat", cfg.HeartbeatInterval.String(), "flush", cfg.FlushInterval.String(),
		"caps", collector.Caps())

	if *dryRun {
		runDryRun(ctx, buf)
		_ = buf.Close()
		return
	}

	// wakeCh: heartbeat 检测到唤醒时发信号；snapshotLoop 收到后立即补快照
	wakeCh := make(chan struct{}, 1)
	// gate: snapshotReady 后才允许 flush 增量；启动时先发一次快照
	gate := newSnapshotGate()

	var wg sync.WaitGroup
	wg.Add(4)
	go func() { defer wg.Done(); heartbeatLoop(ctx, cfg, collector, client, wakeCh) }()
	go func() { defer wg.Done(); flushLoop(ctx, cfg, buf, client, gate) }()
	go func() { defer wg.Done(); snapshotLoop(ctx, cfg, collector, client, gate, wakeCh) }()
	go func() { defer wg.Done(); listenWakes(wakeCh, func() {}) }()

	<-ctx.Done()
	slog.Info("shutdown signal received, draining")

	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
		slog.Info("agent stopped cleanly")
	case <-time.After(5 * time.Second):
		slog.Warn("shutdown timeout, exiting with in-flight requests")
	}
	_ = buf.Close()
}

// openBuffer 按配置打开缓冲实现。SQLite 是生产路径；Memory 仅测试用。
func openBuffer(cfg *config.Config) (buffer.Buffer, error) {
	if cfg.BufferKind == "memory" {
		return buffer.NewMemory(10_000), nil
	}
	path := cfg.BufferPath
	if path == "" {
		// 默认放状态目录下，与 next_seq 一起持久化
		if err := os.MkdirAll(cfg.StatePath, 0o700); err != nil {
			return nil, fmt.Errorf("create state dir: %w", err)
		}
		path = filepath.Join(cfg.StatePath, "buffer.db")
	}
	return buffer.NewSQLite(path, cfg.BufferMaxBytes, cfg.BufferMaxAge)
}

// runDryRun 打印待发批次与 dropped，便于调试"seq 重启不倒退"。
func runDryRun(ctx context.Context, buf buffer.Buffer) {
	batch, err := buf.Peek(500)
	if err != nil {
		slog.Info("dry-run: 缓冲为空", "err", err)
		return
	}
	slog.Info("dry-run: 待发批次",
		"from_seq", batch.FromSeq, "to_seq", batch.ToSeq,
		"count", len(batch.Events), "dropped", batch.Dropped)
	for i, ev := range batch.Events {
		slog.Info("dry-run: event",
			"i", i, "seq", batch.FromSeq+uint64(i),
			"kind", ev.Kind, "task", ev.Task, "occur", ev.Occur)
	}
}

func heartbeatLoop(ctx context.Context, cfg *config.Config, c *collect.Collector, cl *transport.Client, wakeCh chan<- struct{}) {
	ticker := time.NewTicker(cfg.HeartbeatInterval)
	defer ticker.Stop()

	detector := newWakeDetector(cfg.HeartbeatInterval)
	signalWake := func() {
		select {
		case wakeCh <- struct{}{}:
		default: // 已有未处理 wake 信号，不再压栈
		}
	}

	send := func() {
		if detector.observe(time.Now()) {
			signalWake()
		}
		hb := c.Heartbeat(0, 0, 0)
		if err := cl.Post(ctx, "/devices/heartbeat", hb); err != nil {
			slog.Warn("heartbeat failed", "err", err)
			return
		}
		slog.Debug("heartbeat sent", "seq", hb.Seq)
	}

	send()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			send()
		}
	}
}

func flushLoop(ctx context.Context, cfg *config.Config, buf buffer.Buffer, cl *transport.Client, gate *snapshotGate) {
	ticker := time.NewTicker(cfg.FlushInterval)
	defer ticker.Stop()
	loggedOffline := false

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			// 唤醒后必须等快照成功才能 flush 增量——避免老事件先到、快照覆盖矛盾
			if !gate.wait(ctx.Done()) {
				return
			}
			// 离线/在线状态转换打一行日志，避免"一直失败"看不出来
			if cl.IsOffline() && !loggedOffline {
				slog.Warn("离线中：事件留在本地队列，恢复后自动续传", "buffer", cfg.BufferKind)
				loggedOffline = true
			} else if !cl.IsOffline() && loggedOffline {
				slog.Info("已恢复在线")
				loggedOffline = false
			}

			batch, err := buf.Peek(500)
			if errors.Is(err, buffer.ErrEmpty) {
				continue
			}
			if err != nil {
				slog.Warn("peek failed", "err", err)
				continue
			}
			batch.Node = cfg.NodeID
			if err := cl.Post(ctx, "/devices/events", batch); err != nil {
				// 不 Ack：下一轮原样重发，服务端按 (node,seq) 去重
				slog.Warn("event flush failed", "from", batch.FromSeq, "to", batch.ToSeq, "err", err)
				continue
			}
			if err := buf.Ack(batch.ToSeq); err != nil {
				slog.Warn("ack failed", "err", err)
			}
		}
	}
}

func snapshotLoop(ctx context.Context, cfg *config.Config, c *collect.Collector, cl *transport.Client, gate *snapshotGate, wakeCh <-chan struct{}) {
	ticker := time.NewTicker(cfg.SnapshotInterval)
	defer ticker.Stop()

	// 启动即"唤醒"：先发快照再开 flush
	send := func() {
		// 快照发送前关闭 flush 通道（让 flushLoop 阻塞在 gate.wait）
		probe := collect.NewOrcaProbe()
		sessions := probe.Sessions(ctx)
		orcaVer := probe.Version(ctx)
		snap := c.Snapshot(sessions, orcaVer, -1)
		if err := cl.Post(ctx, "/devices/snapshot", snap); err != nil {
			slog.Warn("snapshot failed, incrementals blocked", "err", err)
			return
		}
		slog.Info("snapshot sent, incrementals unblocked",
			"sessions", len(sessions), "orca", orcaVer)
		gate.markReady()
	}

	send()
	for {
		select {
		case <-ctx.Done():
			return
		case <-wakeCh:
			// 唤醒：重置 gate（让 flush 阻塞），重发快照，再开 gate
			gate.reset()
			send()
		case <-ticker.C:
			gate.reset()
			send()
		}
	}
}

var _ = protocol.SchemaVersion
