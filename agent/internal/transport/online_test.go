package transport

import (
	"testing"
	"time"
)

func TestOnlineStateStartsOnline(t *testing.T) {
	s := newOnlineState()
	if s.IsOffline() {
		t.Fatal("初始应为 online")
	}
}

func TestOnlineStateBackoffCaps(t *testing.T) {
	s := newOnlineState()
	s.maxBackoff = 100 * time.Millisecond
	var last time.Duration
	for i := 0; i < 20; i++ {
		b := s.nextBackoff()
		if b > s.maxBackoff {
			t.Fatalf("退避超过上限: %v > %v", b, s.maxBackoff)
		}
		last = b
	}
	if last != s.maxBackoff {
		t.Fatalf("退避应封顶到 %v，实得 %v", s.maxBackoff, last)
	}
}

func TestOnlineStateBackoffResetsOnSuccess(t *testing.T) {
	s := newOnlineState()
	_ = s.nextBackoff()
	_ = s.nextBackoff()
	if s.currentBackoff == 0 {
		t.Fatal("退避应有累积")
	}
	s.markSuccess()
	if s.currentBackoff != 0 {
		t.Fatalf("成功后退避应清零，实得 %v", s.currentBackoff)
	}
}

func TestOnlineStateGoesOfflineAfterThreshold(t *testing.T) {
	s := newOnlineState()
	s.offlineThreshold = 10 * time.Millisecond
	// 模拟"上次成功在很久以前"
	s.lastSuccess = time.Now().Add(-time.Hour)
	if !s.markFailure() {
		t.Fatal("超阈值失败应进入 offline 并返回 true")
	}
	if !s.IsOffline() {
		t.Fatal("应处于 offline")
	}
	// 二次失败不应再返回 true
	if s.markFailure() {
		t.Fatal("已在 offline，二次失败不应返回 true")
	}
}

func TestOnlineStateRecoversOnSuccess(t *testing.T) {
	s := newOnlineState()
	s.offlineThreshold = 10 * time.Millisecond
	s.lastSuccess = time.Now().Add(-time.Hour)
	_ = s.markFailure()
	s.markSuccess()
	if s.IsOffline() {
		t.Fatal("成功后应切回 online")
	}
}
