// Package collect 采集心跳与能力标签。
//
// 平台指标（load/mem/disk/battery）由 metrics_*.go 按平台提供；
// 采集失败绝不影响进程存活——未知指标一律返回 -1（不要用 0，0 会被当成"空闲"）。
package collect

import (
	"os/exec"
	"runtime"
	"time"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

// Collector 组装心跳与快照。metricsFn 可注入假实现用于单测。
type Collector struct {
	NodeID    string
	AgentVer  string
	started   time.Time
	caps      []string
	metricsFn func() Metrics
	diskPath  string
}

func New(nodeID, agentVer string) *Collector {
	return NewWithDiskPath(nodeID, agentVer, "")
}

// NewWithDiskPath 指定磁盘水位观测路径（通常是状态目录所在卷）。
// 空字符串回退到用户主目录。
func NewWithDiskPath(nodeID, agentVer, diskPath string) *Collector {
	if diskPath == "" {
		diskPath = homeDir()
	}
	c := &Collector{
		NodeID:    nodeID,
		AgentVer:  agentVer,
		started:   time.Now(),
		metricsFn: safeDefaultMetrics(diskPath),
		diskPath:  diskPath,
	}
	c.caps = detectCaps()
	return c
}

func (c *Collector) Caps() []string { return c.caps }

// SetMetricsFn 替换指标来源（仅测试用）。
func (c *Collector) SetMetricsFn(fn func() Metrics) { c.metricsFn = fn }

// Heartbeat 组装一次心跳。未知指标用 -1 表示"未知"，服务端据此不做容量假设。
// 任何采集 panic 都被本方法兜住——SetMetricsFn 可注入任意实现，
// "采集失败不影响进程存活"是硬约束，而不是约定。
func (c *Collector) Heartbeat(seq uint64, sessions, queue int) (hb protocol.Heartbeat) {
	hb = protocol.Heartbeat{
		Node:     c.NodeID,
		TS:       time.Now().Unix(),
		Seq:      seq,
		Load:     -1,
		Mem:      -1,
		Disk:     -1,
		Battery:  -1,
		Sessions: sessions,
		Queue:    queue,
		AgentVer: c.AgentVer,
	}
	defer func() {
		if r := recover(); r != nil {
			// 保持 hb 全 -1，仅记不住的值。日志在调用侧按需打印。
			_ = r
		}
	}()
	m := c.metricsFn()
	hb.Load, hb.Mem, hb.Disk, hb.Battery = m.Load, m.Mem, m.Disk, m.Battery
	return hb
}

func (c *Collector) Snapshot(sessions []protocol.Session, orcaVer string, diskUsedGB float64) protocol.Snapshot {
	return protocol.Snapshot{
		Node: c.NodeID,
		TS:   time.Now().Unix(),
		Versions: protocol.Versions{
			Agent: c.AgentVer,
			Orca:  orcaVer,
		},
		Sessions: sessions,
		Caps:     c.caps,
		DiskUsed: diskUsedGB,
	}
}

// safeDefaultMetrics 仅做兜底；Heartbeat 自身也有 recover，
// 这样即便上层通过 SetMetricsFn 注入了会 panic 的实现，进程也活得了。
func safeDefaultMetrics(diskPath string) func() Metrics {
	inner := defaultMetrics(diskPath)
	return func() (m Metrics) {
		defer func() {
			if r := recover(); r != nil {
				m = UnknownMetrics()
			}
		}()
		return inner()
	}
}

// detectCaps 探测能力标签；标签决定服务端派什么活，因此宁可少报不可多报。
func detectCaps() []string {
	caps := []string{"os:" + runtime.GOOS, "arch:" + runtime.GOARCH}

	if runtime.GOOS == "darwin" || runtime.GOOS == "windows" {
		caps = append(caps, "has_gui")
	}
	if _, err := exec.LookPath("orca"); err == nil {
		caps = append(caps, "orca")
	}
	if _, err := exec.LookPath("docker"); err == nil {
		caps = append(caps, "docker")
	}
	if _, err := exec.LookPath("adb"); err == nil {
		caps = append(caps, "android")
	}
	// always_on 不是探测出来的，是部署时人工声明的（笔记本休眠派活是最常见的坑）
	return caps
}
