package buffer

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"

	// 纯 Go SQLite 驱动，免 CGO，保跨平台交叉编译。
	_ "modernc.org/sqlite"
)

// SQLite 实现 Buffer 接口。
//
// 三条不可变语义（与 Memory 一致）：
//  1. Seq 单调递增、跨重启不倒退——next_seq 持久化在 meta 表
//  2. 超限丢最旧事件并累加 dropped_count
//  3. Peek 看未 Ack 的最早一批；Ack 成功才推进游标
//
// 超限判定按"字节数"和"年龄"双重：超过 BufferMaxBytes 删最旧；
// 超过 BufferMaxAge 的已 Ack 行也清掉（保留未 Ack 行以免丢未发数据）。
type SQLite struct {
	mu       sync.Mutex
	db       *sql.DB
	maxBytes int64
	maxAge   time.Duration

	// 缓存：避免每次 Seq()/Dropped() 都查库
	nextSeq  uint64
	dropped  uint64 // 自上次 Ack 后新增的丢失数；Ack 时清零并随批次上报
}

// NewSQLite 打开（必要时创建）一个 SQLite 缓冲。
// path 为空时使用 "<statePath>/buffer.db"。
func NewSQLite(path string, maxBytes int64, maxAge time.Duration) (*SQLite, error) {
	if path == "" {
		return nil, errors.New("buffer path 不能为空")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("创建缓冲目录: %w", err)
	}
	// _pragma 串：WAL 提升并发；busy_timeout 防锁失败；foreign_keys 关（无需）。
	dsn := "file:" + path + "?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=synchronous(NORMAL)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// SQLite 单连接足够（设备端写入量小，多连接反而增锁开销）
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	s := &SQLite{db: db, maxBytes: maxBytes, maxAge: maxAge}
	if err := s.init(context.Background()); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLite) init(ctx context.Context) error {
	// events 存未 Ack 的事件（待发）；seq 单调递增且有索引便于按序读
	// meta 存 next_seq / dropped_count，跨重启不丢
	schema := `
CREATE TABLE IF NOT EXISTS events (
    seq    INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,
    body   BLOB NOT NULL,
    occur  INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'  -- pending / sent
);
CREATE INDEX IF NOT EXISTS idx_events_state_seq ON events(state, seq);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
`
	if _, err := s.db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("init schema: %w", err)
	}

	// 加载 next_seq：没有则初始化为 1（首条事件 seq=1）
	var nextSeq uint64 = 1
	row := s.db.QueryRowContext(ctx, "SELECT value FROM meta WHERE key=?", "next_seq")
	switch err := row.Scan(&nextSeq); {
	case errors.Is(err, sql.ErrNoRows):
		if _, err := s.db.ExecContext(ctx, "INSERT INTO meta(key,value) VALUES('next_seq',1)"); err != nil {
			return fmt.Errorf("init next_seq: %w", err)
		}
	case err != nil:
		return fmt.Errorf("load next_seq: %w", err)
	}
	s.nextSeq = nextSeq

	// 启动时把上次"已发但未 Ack"的残留事件回滚为 pending
	// （kill -9 在 Post 与 Ack 之间发生时，sent 行应被重发，幂等由服务端 (node,seq) 保证）
	if _, err := s.db.ExecContext(ctx, "UPDATE events SET state='pending' WHERE state='sent'"); err != nil {
		return fmt.Errorf("reset sent->pending: %w", err)
	}

	// dropped_count 持久累计（用于断电前后一致性）；运行期 Ack 清零
	var dropped uint64
	if err := s.db.QueryRowContext(ctx,
		"SELECT value FROM meta WHERE key=?", "dropped_count").Scan(&dropped); err == nil {
		s.dropped = dropped
	}
	return s.enforceLimits(ctx)
}

func (s *SQLite) Seq() uint64 {
	return atomic.LoadUint64(&s.nextSeq) - 1
}

func (s *SQLite) Append(events ...protocol.Event) (uint64, uint64, error) {
	if len(events) == 0 {
		return 0, 0, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, 0, fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	from := s.nextSeq
	to := from + uint64(len(events)) - 1
	now := time.Now().Unix()
	for i, ev := range events {
		body, err := marshalEvent(ev)
		if err != nil {
			return 0, 0, fmt.Errorf("marshal event[%d]: %w", i, err)
		}
		occur := ev.Occur
		if occur == 0 {
			occur = now
		}
		if _, err := tx.ExecContext(ctx,
			"INSERT INTO events(seq,kind,body,occur,state) VALUES(?,?,?,?, 'pending')",
			from+uint64(i), ev.Kind, body, occur); err != nil {
			return 0, 0, fmt.Errorf("insert event seq=%d: %w", from+uint64(i), err)
		}
	}

	if _, err := tx.ExecContext(ctx,
		"UPDATE meta SET value=? WHERE key='next_seq'", to+1); err != nil {
		return 0, 0, fmt.Errorf("bump next_seq: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return 0, 0, fmt.Errorf("commit append: %w", err)
	}
	s.nextSeq = to + 1

	// 超限在事务外执行（不影响 Append 的原子性）
	_ = s.enforceLimits(ctx)
	return from, to, nil
}

func (s *SQLite) Peek(max int) (protocol.EventBatch, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	limit := max
	if limit <= 0 {
		limit = 500
	}
	rows, err := s.db.QueryContext(ctx,
		"SELECT seq, kind, body, occur FROM events WHERE state='pending' ORDER BY seq ASC LIMIT ?",
		limit)
	if err != nil {
		return protocol.EventBatch{}, fmt.Errorf("peek: %w", err)
	}
	defer rows.Close()

	batch := protocol.EventBatch{Dropped: s.dropped}
	first := true
	for rows.Next() {
		var seq uint64
		var kind string
		var body []byte
		var occur int64
		if err := rows.Scan(&seq, &kind, &body, &occur); err != nil {
			return protocol.EventBatch{}, fmt.Errorf("scan: %w", err)
		}
		ev, err := unmarshalEvent(body)
		if err != nil {
			return protocol.EventBatch{}, fmt.Errorf("unmarshal seq=%d: %w", seq, err)
		}
		ev.Kind = kind
		if ev.Occur == 0 {
			ev.Occur = occur
		}
		batch.Events = append(batch.Events, ev)
		if first {
			batch.FromSeq = seq
			first = false
		}
		batch.ToSeq = seq
	}
	if err := rows.Err(); err != nil {
		return protocol.EventBatch{}, fmt.Errorf("rows iter: %w", err)
	}
	if len(batch.Events) == 0 {
		return protocol.EventBatch{}, ErrEmpty
	}

	// 标记为 sent（kill -9 后重启会发现 sent 行回滚为 pending 重发）
	if _, err := s.db.ExecContext(ctx,
		"UPDATE events SET state='sent' WHERE seq BETWEEN ? AND ?",
		batch.FromSeq, batch.ToSeq); err != nil {
		return protocol.EventBatch{}, fmt.Errorf("mark sent: %w", err)
	}
	return batch, nil
}

func (s *SQLite) Ack(toSeq uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx,
		"DELETE FROM events WHERE seq<=? AND state='sent'", toSeq); err != nil {
		return fmt.Errorf("delete acked: %w", err)
	}
	// 累计 dropped 已随本批上报，清零并持久化
	if s.dropped > 0 {
		if _, err := tx.ExecContext(ctx,
			"INSERT INTO meta(key,value) VALUES('dropped_count',0) " +
			"ON CONFLICT(key) DO UPDATE SET value=0"); err != nil {
			return fmt.Errorf("reset dropped: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit ack: %w", err)
	}
	s.dropped = 0
	return nil
}

func (s *SQLite) Dropped() uint64 {
	return atomic.LoadUint64(&s.dropped)
}

func (s *SQLite) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.db == nil {
		return nil
	}
	return s.db.Close()
}

// enforceLimits 超限丢最旧并累加 dropped。
// 调用方已持锁。两个触发条件：
//  1. 字节数超 maxBytes：删最早的 pending 行直到恢复到阈值以下
//  2. 年龄超 maxAge：删所有超龄的 sent 行（pending 行若也超龄，因未发出不删）
//
// 注意：删 pending 行才算"丢"，必须累加 dropped_count 并持久化。
func (s *SQLite) enforceLimits(ctx context.Context) error {
	if s.maxBytes <= 0 && s.maxAge <= 0 {
		return nil
	}

	// 1. 年龄清理（只删 sent 行）
	if s.maxAge > 0 {
		cutoff := time.Now().Add(-s.maxAge).Unix()
		if _, err := s.db.ExecContext(ctx,
			"DELETE FROM events WHERE occur<? AND state='sent'", cutoff); err != nil {
			return fmt.Errorf("age purge: %w", err)
		}
	}

	// 2. 字节清理：删最早的 pending 行（即"丢最旧"），累加 dropped
	if s.maxBytes > 0 {
		for {
			var size int64
			if err := s.db.QueryRowContext(ctx,
				"SELECT COALESCE(SUM(LENGTH(body)),0) FROM events").Scan(&size); err != nil {
				return fmt.Errorf("meaure: %w", err)
			}
			if size <= s.maxBytes {
				break
			}
			// 删最早的一行 pending（不论它有多老，丢最旧就是丢最早的未发事件）
			res, err := s.db.ExecContext(ctx,
				"DELETE FROM events WHERE seq=(SELECT MIN(seq) FROM events WHERE state='pending')")
			if err != nil {
				return fmt.Errorf("drop oldest: %w", err)
			}
			n, _ := res.RowsAffected()
			if n == 0 {
				break // 没有 pending 行可删了（只剩 sent 行待 Ack）
			}
			s.dropped++
		}

		if s.dropped > 0 {
			if _, err := s.db.ExecContext(ctx,
				"INSERT INTO meta(key,value) VALUES('dropped_count',?) " +
				"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
				s.dropped); err != nil {
				return fmt.Errorf("persist dropped: %w", err)
			}
		}
	}
	return nil
}

// marshalEvent / unmarshalEvent：事件体序列化为 blob 存库。
// 直接用 encoding/json，与 transport 层一致，不需要额外依赖。
func marshalEvent(ev protocol.Event) ([]byte, error) {
	return json.Marshal(ev)
}

func unmarshalEvent(body []byte) (protocol.Event, error) {
	var ev protocol.Event
	if err := json.Unmarshal(body, &ev); err != nil {
		return protocol.Event{}, err
	}
	return ev, nil
}