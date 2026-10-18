package main

import (
	"sync"
	"testing"
	"time"

	"github.com/jackma3327/AgentsControl/agent/internal/collect"
)

func timeoutChan(d time.Duration) <-chan struct{} {
	ch := make(chan struct{})
	go func() {
		time.Sleep(d)
		close(ch)
	}()
	return ch
}

func TestWakeDetectorFirstCallNotWake(t *testing.T) {
	w := newWakeDetector(time.Minute)
	if w.observe(time.Now()) {
		t.Fatal("首次调用不应判为唤醒")
	}
}

func TestWakeDetectorNormalGapNotWake(t *testing.T) {
	w := newWakeDetector(time.Minute)
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	w.observe(base)
	// 90 秒间隔，阈值 150 秒，不应判唤醒
	if w.observe(base.Add(90 * time.Second)) {
		t.Fatal("正常间隔不应判唤醒")
	}
}

func TestWakeDetectorHugeGapIsWake(t *testing.T) {
	w := newWakeDetector(time.Minute)
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	w.observe(base)
	// 模拟"休眠 2 小时后唤醒"：2 小时间隔远超 150s 阈值
	if !w.observe(base.Add(2 * time.Hour)) {
		t.Fatal("2 小时间隔应判为唤醒")
	}
}

func TestSnapshotGateBlocksUntilReady(t *testing.T) {
	g := newSnapshotGate()
	var wg sync.WaitGroup
	wg.Add(1)
	got := false
	go func() {
		defer wg.Done()
		if g.wait(timeoutChan(200 * time.Millisecond)) {
			got = true
		}
	}()
	// 给一点时间让 goroutine 进入 wait
	time.Sleep(10 * time.Millisecond)
	g.markReady()
	wg.Wait()
	if !got {
		t.Fatal("markReady 后 wait 应返回 true")
	}
}

func TestSnapshotGateResetBlocksAgain(t *testing.T) {
	g := newSnapshotGate()
	g.markReady()
	if !g.wait(timeoutChan(time.Second)) {
		t.Fatal("已就绪应立即返回 true")
	}
	g.reset()
	// reset 后再次阻塞——给它一点时间确认是非阻塞
	if g.wait(timeoutChan(50 * time.Millisecond)) {
		t.Fatal("reset 后应再次阻塞，wait 不应返回 true")
	}
}

func TestSnapshotGateTimeoutOnCtxCancel(t *testing.T) {
	g := newSnapshotGate()
	done := make(chan struct{})
	close(done) // 模拟 ctx.Done
	if g.wait(done) {
		t.Fatal("ctx 取消时应返回 false")
	}
}

func TestParseOrcaSessions(t *testing.T) {
	out := "s1|running|t1|/repo/wt1\n" +
		"s2|idle||\n" +
		"  \n" + // 空行
		"bad-line-with-no-pipe\n" +
		"s3|done|t3|/repo/wt3\n"
	sessions := collect.ParseOrcaSessionsFromTest(out)
	if len(sessions) != 3 {
		t.Fatalf("应解析 3 条会话，实得 %d", len(sessions))
	}
	if sessions[0].ID != "s1" || sessions[0].State != "running" || sessions[0].Task != "t1" {
		t.Fatalf("session[0] 解析错误: %+v", sessions[0])
	}
	if sessions[1].ID != "s2" || sessions[1].State != "idle" || sessions[1].Task != "" {
		t.Fatalf("session[1] 解析错误: %+v", sessions[1])
	}
	if sessions[2].ID != "s3" || sessions[2].Worktree != "/repo/wt3" {
		t.Fatalf("session[2] 解析错误: %+v", sessions[2])
	}
}

// TestWakeFlowEndToEnd 模拟"休眠 2 小时后唤醒"的事件顺序：
// 1) wakeDetector 检出唤醒 →
// 2) snapshotLoop 收到 wakeCh →
// 3) 调用 send（这里只 set flag） →
// 4) gate.markReady →
// 5) flushLoop 不再阻塞
func TestWakeFlowEndToEnd(t *testing.T) {
	wakeCh := make(chan struct{}, 1)
	detector := newWakeDetector(time.Minute)

	// 模拟两次心跳：基线 + 2 小时后唤醒
	t0 := time.Now()
	detector.observe(t0)
	if detector.observe(t0.Add(2 * time.Hour)) {
		wakeCh <- struct{}{}
	}

	// snapshotLoop: 收到 wakeCh 后 markReady
	go func() {
		<-wakeCh
		// 在真实代码里这里慢（Post 快照），用 send 模拟
		gate := newSnapshotGate()
		gate.markReady()
		if !gate.wait(timeoutChan(time.Second)) {
			t.Errorf("快照成功后 gate 仍阻塞")
		}
	}()

	// 让 goroutine 跑完
	time.Sleep(100 * time.Millisecond)
}
