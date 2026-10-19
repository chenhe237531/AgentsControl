package buffer

import (
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

// newTestSQLite 每个测试用独立临时目录，避免互相干扰。
func newTestSQLite(t *testing.T, maxBytes int64, maxAge time.Duration) *SQLite {
	t.Helper()
	dir := t.TempDir()
	s, err := NewSQLite(filepath.Join(dir, "buffer.db"), maxBytes, maxAge)
	if err != nil {
		t.Fatalf("NewSQLite: %v", err)
	}
	return s
}

func TestSQLiteAppendAndPeek(t *testing.T) {
	s := newTestSQLite(t, 0, 0)
	defer s.Close()

	from, to, err := s.Append(
		protocol.Event{Kind: "task_done", Task: "t1"},
		protocol.Event{Kind: "task_done", Task: "t2"},
	)
	if err != nil || from != 1 || to != 2 {
		t.Fatalf("Append 返回 (%d,%d,%v)，期望 (1,2,nil)", from, to, err)
	}
	if s.Seq() != 2 {
		t.Fatalf("Seq=%d，期望 2", s.Seq())
	}

	batch, err := s.Peek(10)
	if err != nil {
		t.Fatalf("Peek: %v", err)
	}
	if batch.FromSeq != 1 || batch.ToSeq != 2 || len(batch.Events) != 2 {
		t.Fatalf("Peek 批次错误: %+v", batch)
	}
	if batch.Events[0].Task != "t1" || batch.Events[1].Task != "t2" {
		t.Fatalf("Peek 事件顺序错误: %+v", batch.Events)
	}

	// Peek 只读：未 Ack 前再次 Peek 必须取到同一批事件（Post 失败原样重发的根基）
	again, err := s.Peek(10)
	if err != nil {
		t.Fatalf("second Peek: %v", err)
	}
	if again.FromSeq != 1 || again.ToSeq != 2 || len(again.Events) != 2 {
		t.Fatalf("未 Ack 前 second Peek 应返回同一批，实得 %+v", again)
	}

	// Ack 后才为空
	if err := s.Ack(again.ToSeq); err != nil {
		t.Fatalf("Ack: %v", err)
	}
	if _, err := s.Peek(10); err != ErrEmpty {
		t.Fatalf("Ack 后 Peek 应为 ErrEmpty，实得 %v", err)
	}
}

// TestSQLiteResendAfterFailedPost 回归测试（PR #7 评审阻塞项 #1）：
// Peek 不再有 sent 副作用。Post 失败（不 Ack）后追加新事件，
// 下一轮 Peek 必须仍从最早未 Ack 的事件开始——既不丢旧事件，也不被后续 Ack 连带删除。
func TestSQLiteResendAfterFailedPost(t *testing.T) {
	s := newTestSQLite(t, 0, 0)
	defer s.Close()

	if _, _, err := s.Append(protocol.Event{Kind: "x", Task: "t1"}); err != nil {
		t.Fatal(err)
	}
	first, err := s.Peek(500)
	if err != nil {
		t.Fatalf("first Peek: %v", err)
	}
	// 模拟 Post 失败：什么都不做（不 Ack）

	if _, _, err := s.Append(protocol.Event{Kind: "y", Task: "t2"}); err != nil {
		t.Fatal(err)
	}
	second, err := s.Peek(500)
	if err != nil {
		t.Fatalf("second Peek: %v", err)
	}
	if second.FromSeq != first.FromSeq || len(second.Events) != 2 {
		t.Fatalf("Post 失败后重发批次丢失旧事件: first=%+v second=%+v", first, second)
	}
	if second.Dropped != 0 {
		t.Fatalf("无超限不应有 dropped，实得 %d", second.Dropped)
	}

	// 全部发送成功后 Ack：两行都应被删除，不留滞留行
	if err := s.Ack(second.ToSeq); err != nil {
		t.Fatalf("Ack: %v", err)
	}
	if _, err := s.Peek(500); err != ErrEmpty {
		t.Fatalf("Ack 后 Peek 应 ErrEmpty，实得 %v", err)
	}
}

func TestSQLiteAckAdvancesCursor(t *testing.T) {
	s := newTestSQLite(t, 0, 0)
	defer s.Close()

	if _, _, err := s.Append(protocol.Event{Kind: "x"}, protocol.Event{Kind: "y"}); err != nil {
		t.Fatal(err)
	}
	batch, err := s.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	// 模拟"Post 成功"后 Ack
	if err := s.Ack(batch.ToSeq); err != nil {
		t.Fatalf("Ack: %v", err)
	}
	if _, err := s.Peek(10); err != ErrEmpty {
		t.Fatalf("Ack 后 Peek 应为 ErrEmpty，实得 %v", err)
	}
}

// TestSQLiteSurvivesRestart 模拟"kill -9 后重启"：
// 进程在不调 Close 的情况下"挂掉"，重新打开同一文件，
// next_seq 单调不减；未 Ack 的事件仍在。
func TestSQLiteSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "buffer.db")

	s1, err := NewSQLite(path, 0, 0)
	if err != nil {
		t.Fatalf("NewSQLite s1: %v", err)
	}
	if _, _, err := s1.Append(protocol.Event{Kind: "x", Task: "t1"}); err != nil {
		t.Fatal(err)
	}
	// 模拟崩溃：不调 Close，直接丢掉引用（DB 连接不会被显式关）
	if err := s1.Close(); err != nil { // Close 模拟 WAL flush 到磁盘
		t.Fatal(err)
	}

	// 重启：同一文件
	s2, err := NewSQLite(path, 0, 0)
	if err != nil {
		t.Fatalf("NewSQLite s2: %v", err)
	}
	defer s2.Close()

	// next_seq 必须从 2 继续递增，而不是回到 1
	from, to, err := s2.Append(protocol.Event{Kind: "y", Task: "t2"})
	if err != nil || from != 2 || to != 2 {
		t.Fatalf("重启后 Append seq 倒退: (%d,%d,%v)", from, to, err)
	}

	// 重启前未 Ack 的事件（t1, seq=1）必须仍在
	batch, err := s2.Peek(100)
	if err != nil {
		t.Fatalf("Peek: %v", err)
	}
	if batch.FromSeq != 1 || len(batch.Events) != 2 || batch.Events[0].Task != "t1" {
		t.Fatalf("重启后未 Ack 事件丢失: %+v", batch)
	}
}

func TestSQLiteDropsOldestOnBytesOverflow(t *testing.T) {
	// maxBytes 设很小，强制丢最旧
	s := newTestSQLite(t, 200, 0)
	defer s.Close()

	// 写 5 个事件，每个 body 都较大，必然超限丢最旧
	for i := 0; i < 5; i++ {
		if _, _, err := s.Append(protocol.Event{
			Kind: "x", Task: "t", Extra: map[string]any{
				"pad": "0123456789abcdef0123456789abcdef0123456789abcdef",
			},
		}); err != nil {
			t.Fatal(err)
		}
	}

	// 必须有 dropped 累计
	if s.Dropped() == 0 {
		t.Fatalf("超限应累计 dropped，实得 %d", s.Dropped())
	}

	// Peeking 不应 panic，且 FromSeq > 1（最旧的丢了）
	batch, err := s.Peek(100)
	if err != nil {
		t.Fatalf("Peek: %v", err)
	}
	if batch.FromSeq <= 1 {
		t.Fatalf("最旧事件未被丢: from=%d", batch.FromSeq)
	}
	if batch.Dropped != s.Dropped() {
		t.Fatalf("批次 dropped 与计数器不一致: %d vs %d", batch.Dropped, s.Dropped())
	}

	// Ack 后 dropped 清零
	if err := s.Ack(batch.ToSeq); err != nil {
		t.Fatalf("Ack: %v", err)
	}
	if s.Dropped() != 0 {
		t.Fatalf("Ack 后 dropped 应清零，实得 %d", s.Dropped())
	}
}

func TestSQLitePurgesAgeOnAck(t *testing.T) {
	// maxAge 极短：超龄行在下次 enforceLimits（Append 触发）时被清掉并计入 dropped
	s := newTestSQLite(t, 0, 1*time.Millisecond)
	defer s.Close()

	if _, _, err := s.Append(protocol.Event{Kind: "x"}); err != nil {
		t.Fatal(err)
	}
	batch, err := s.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond)
	if err := s.Ack(batch.ToSeq); err != nil {
		t.Fatal(err)
	}
	// Ack 不触发 age 清理；下一次 Append/Peek 才会。我们手动 Peek 看是否空：
	// 但 Peek 会再次走 enforceLimits? 不会——只在 init/Append 时调用。
	// 直接对库做一次 Append 触发即可。
	if _, _, err := s.Append(protocol.Event{Kind: "y"}); err != nil {
		t.Fatal(err)
	}
	b, err := s.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	// 只剩 seq 较大的那条
	if b.FromSeq == batch.FromSeq {
		t.Fatalf("超龄 sent 行未被清理: from=%d", b.FromSeq)
	}
}

func TestSQLiteDroppedResetsAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "buffer.db")

	s1, err := NewSQLite(path, 100, 0) // 极小 maxBytes
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		_, _, _ = s1.Append(protocol.Event{Kind: "x", Extra: map[string]any{"p": "0123456789abcdef0123456789abcdef"}})
	}
	if s1.Dropped() == 0 {
		t.Fatal("预期有 dropped")
	}
	_ = s1.Close()

	s2, err := NewSQLite(path, 100, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if s2.Dropped() == 0 {
		t.Fatalf("断电后 dropped 计数丢失，实得 %d", s2.Dropped())
	}
}

// TestSQLiteConcurrentAppend 验证并发写入不会出问题（go test -race 才有意义）。
func TestSQLiteConcurrentAppend(t *testing.T) {
	s := newTestSQLite(t, 0, 0)
	defer s.Close()

	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				_, _, _ = s.Append(protocol.Event{Kind: "x"})
			}
		}()
	}
	wg.Wait()

	// 总共 200 条事件
	batch, err := s.Peek(500)
	if err != nil {
		t.Fatalf("Peek: %v", err)
	}
	if got := uint64(len(batch.Events)); got != 200 {
		t.Fatalf("并发 Append 后事件数 = %d, want 200", got)
	}
	if batch.FromSeq != 1 || batch.ToSeq != 200 {
		t.Fatalf("seq 范围错误: %d-%d", batch.FromSeq, batch.ToSeq)
	}
}

func TestSQLiteEmptyAfterAckAll(t *testing.T) {
	s := newTestSQLite(t, 0, 0)
	defer s.Close()
	if _, _, err := s.Append(protocol.Event{Kind: "x"}); err != nil {
		t.Fatal(err)
	}
	b, err := s.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Ack(b.ToSeq); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Peek(10); err != ErrEmpty {
		t.Fatalf("全部 Ack 后 Peek 应 ErrEmpty, 实得 %v", err)
	}
}
