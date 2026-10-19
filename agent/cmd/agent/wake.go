// Package main · 运行期协调：唤醒后"先快照再增量"。
//
// 设计：
//   - heartbeatLoop 记录上次心跳时间；若两次心跳间隔显著超过 HeartbeatInterval（默认 2.5x），
//     说明系统刚从睡眠唤醒，触发 wakeup 信号
//   - snapshotLoop 收到 wakeup 信号立即补发快照
//   - snapshot 成功后开 snapshotReady 通道，flushLoop 必须等到 ready 才发增量
//   - 启动时刚启动也走一次 wake 流程，避免漏掉启动前发生的事件
package main

import (
	"sync"
	"time"
)

// snapshotGate 是一个一次性的"快照已就绪"信号。
// 唤醒/启动后先发快照，快照 200 后才允许 flush 增量。
// nextSnapshotGate 创建一个新的 gate（已 close 的 channel 表示已就绪）。
type snapshotGate struct {
	mu      sync.Mutex
	readyCh chan struct{}
}

func newSnapshotGate() *snapshotGate {
	g := &snapshotGate{readyCh: make(chan struct{})}
	return g
}

// wait 阻塞到快照就绪或 ctx 取消（ctx 取消返回 false）。
func (g *snapshotGate) wait(done <-chan struct{}) bool {
	g.mu.Lock()
	ch := g.readyCh
	g.mu.Unlock()
	select {
	case <-ch:
		return true
	case <-done:
		return false
	}
}

// markReady 关闭当前 gate；多次调用安全（close 后再 close 会 panic，需 swap）。
func (g *snapshotGate) markReady() {
	g.mu.Lock()
	defer g.mu.Unlock()
	select {
	case <-g.readyCh:
		return // 已就绪
	default:
		close(g.readyCh)
	}
}

// reset 重置为新的未就绪 gate。snapshotLoop 在检测到唤醒后调用，
// 使 flushLoop 再次阻塞直到新一轮快照成功发出。
func (g *snapshotGate) reset() {
	g.mu.Lock()
	defer g.mu.Unlock()
	select {
	case <-g.readyCh:
		// 已就绪，需要重新关闭
	default:
		return // 还没就绪，无需 reset
	}
	g.readyCh = make(chan struct{})
}

// wakeDetector 监测"心跳间隔异常增大"——一般来自系统睡眠唤醒。
// macOS NSWorkspaceDidWake 需 CGO/ObjC，本窗口坚持无 CGO，
// 因此用心跳间隔启发式覆盖 99% 场景（睡眠时心跳不发，醒来后首跳间隔远超周期）。
type wakeDetector struct {
	threshold time.Duration
	last      time.Time
}

// observe 检查本次心跳间隔是否暗示睡眠唤醒。
// 第一次调用返回 false（系统时间起点不算唤醒）。
func (w *wakeDetector) observe(now time.Time) bool {
	if w.last.IsZero() {
		w.last = now
		return false
	}
	gap := now.Sub(w.last)
	w.last = now
	return gap > w.threshold
}

// newWakeDetector 用心跳周期构造唤醒阈值。
// 阈值取 2.5x：1 次正常抖动（≈1.0x）不会误判，
// 但 2.5x 一般只可能由系统睡眠造成（heartbeat 60s → 阈值 150s）。
func newWakeDetector(heartbeat time.Duration) *wakeDetector {
	return &wakeDetector{threshold: heartbeat * 5 / 2}
}
