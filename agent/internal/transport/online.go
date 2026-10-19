// Package transport · 在线状态机 + 退避。
//
// 设计目标（W05-I06）：
//   - 完全断网 1 小时后恢复，事件一条不丢（已由 SQLite Buffer + 不 Ack 保证）
//   - 重试退避上限 5 min（避免风暴）
//   - 日志能明确看出"离线中"而不是"一直失败"
//
// 状态：
//
//	online  —— 最近一次请求成功
//	offline —— 连续失败超 offlineThreshold（默认 30s）；恢复成功立刻切回 online
package transport

import (
	"sync"
	"time"
)

type onlineState struct {
	offlineThreshold time.Duration
	maxBackoff       time.Duration

	// heartbeat/flush/snapshot 三个循环共用同一个 Client 并发调用，
	// 全部状态必须在锁内读写，否则 -race 报数据竞争。
	mu sync.Mutex

	lastSuccess time.Time
	lastFailure time.Time

	currentBackoff time.Duration
	isOffline      bool
}

func newOnlineState() *onlineState {
	return &onlineState{
		offlineThreshold: 30 * time.Second,
		maxBackoff:       5 * time.Minute,
		lastSuccess:      time.Now(),
	}
}

// nextBackoff 返回下次重试应等待的时长。
// 指数退避：500ms → 1s → 2s → 4s → 8s → 16s → 32s → 1m → 2m → 4m → 5m（封顶）。
func (s *onlineState) nextBackoff() time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.currentBackoff == 0 {
		s.currentBackoff = 500 * time.Millisecond
	} else {
		s.currentBackoff *= 2
	}
	if s.currentBackoff > s.maxBackoff {
		s.currentBackoff = s.maxBackoff
	}
	return s.currentBackoff
}

// markSuccess 记录成功并切回 online。
func (s *onlineState) markSuccess() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastSuccess = time.Now()
	s.currentBackoff = 0
	if s.isOffline {
		s.isOffline = false
	}
}

// markFailure 记录失败，超过阈值则进入 offline。
// 返回 (justWentOffline bool) 让调用方决定是否打日志。
func (s *onlineState) markFailure() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastFailure = time.Now()
	if !s.isOffline && time.Since(s.lastSuccess) > s.offlineThreshold {
		s.isOffline = true
		return true // 刚进入 offline
	}
	return false
}

func (s *onlineState) IsOffline() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.isOffline
}
