// Package buffer 本地事件缓冲。
//
// 骨架用内存实现；W05 替换为 SQLite（断电不丢、支持游标续传）。
// 无论哪种实现，三条语义不可变：
//  1. Seq 单调递增，跨重启不倒退（持久化 next_seq）
//  2. 超限丢**最旧**事件，并累加 Dropped（必须上报）
//  3. Peek/Take 语义：发送成功后才推进游标，失败可重发
package buffer

import (
	"errors"
	"sync"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

var ErrEmpty = errors.New("buffer empty")

// ErrDiskPressure 磁盘水位 > 90% 时 Append 返回此错误。
// 调用方应上报 disk_pressure 事件并停止写入，直到水位恢复。
var ErrDiskPressure = errors.New("disk pressure")

type Buffer interface {
	Seq() uint64
	Append(events ...protocol.Event) (fromSeq, toSeq uint64, err error)
	Peek(max int) (protocol.EventBatch, error)
	// Ack 提交已成功发送的批次；游标只前进，不回退
	Ack(toSeq uint64) error
	Dropped() uint64
	Close() error
}

type Memory struct {
	mu      sync.Mutex
	nextSeq uint64
	items   []seqEvent
	max     int
	dropped uint64
}

type seqEvent struct {
	seq uint64
	ev  protocol.Event
}

func NewMemory(maxItems int) *Memory {
	if maxItems <= 0 {
		maxItems = 10_000
	}
	return &Memory{nextSeq: 1, max: maxItems}
}

func (m *Memory) Seq() uint64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.nextSeq - 1
}

func (m *Memory) Append(events ...protocol.Event) (uint64, uint64, error) {
	if len(events) == 0 {
		return 0, 0, nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()

	from := m.nextSeq
	for _, ev := range events {
		m.items = append(m.items, seqEvent{seq: m.nextSeq, ev: ev})
		m.nextSeq++
	}
	// 超限丢弃最旧并计数
	if over := len(m.items) - m.max; over > 0 {
		m.items = m.items[over:]
		m.dropped += uint64(over)
	}
	return from, m.nextSeq - 1, nil
}

func (m *Memory) Peek(max int) (protocol.EventBatch, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.items) == 0 {
		return protocol.EventBatch{}, ErrEmpty
	}
	if max > 0 && max < len(m.items) {
		m.items = m.items[:max]
	}
	batch := protocol.EventBatch{
		FromSeq: m.items[0].seq,
		ToSeq:   m.items[len(m.items)-1].seq,
		Events:  make([]protocol.Event, len(m.items)),
		Dropped: m.dropped,
	}
	for i, it := range m.items {
		batch.Events[i] = it.ev
	}
	return batch, nil
}

func (m *Memory) Ack(toSeq uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	cut := 0
	for cut < len(m.items) && m.items[cut].seq <= toSeq {
		cut++
	}
	m.items = m.items[cut:]
	if cut > 0 {
		m.dropped = 0 // 已随本批上报
	}
	return nil
}

func (m *Memory) Dropped() uint64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.dropped
}

func (m *Memory) Close() error { return nil }
