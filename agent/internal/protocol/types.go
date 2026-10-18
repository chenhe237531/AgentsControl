// Package protocol 定义设备与服务端之间的线格式。
//
// 这是**跨端契约**：字段名一旦发布不得改名（服务端按此解析），
// 变更只能新增可选字段并提升 schema 版本。
// 服务端对应实现：backend/app/modules/devices（窗口 W04）。
package protocol

// SchemaVersion 与服务端核对；不一致时设备端应上报一次 snapshot 并记录告警。
const SchemaVersion = 1

// Heartbeat 60s 一次，<1KB。任何字段缺失都视为设备能力未知，不派活。
type Heartbeat struct {
	Node     string  `json:"node"`
	TS       int64   `json:"ts"`
	Seq      uint64  `json:"seq"`
	Load     float64 `json:"load"`
	Mem      float64 `json:"mem"`
	Disk     float64 `json:"disk"`
	Battery  float64 `json:"battery"`
	Sessions int     `json:"sessions"`
	Queue    int     `json:"queue"`
	AgentVer string  `json:"agent_ver"`
}

// Event 增量事件。Kind 枚举由服务端白名单校验，未知 kind 拒收并计数。
type Event struct {
	Kind  string         `json:"kind"`
	Task  string         `json:"task,omitempty"`
	Node  string         `json:"node,omitempty"`
	Model string         `json:"model,omitempty"`
	Cost  float64        `json:"cost,omitempty"`
	MS    int64          `json:"ms,omitempty"`
	Extra map[string]any `json:"extra,omitempty"`
	Occur int64          `json:"occurred_at,omitempty"`
}

// EventBatch 30s 批量，游标续传。服务端按 (node, seq) 唯一索引去重。
type EventBatch struct {
	Node    string  `json:"node"`
	FromSeq uint64  `json:"from_seq"`
	ToSeq   uint64  `json:"to_seq"`
	Events  []Event `json:"events"`
	// Dropped 因缓冲区上限被丢弃的事件数。**必须上报，不能静默丢**。
	Dropped uint64 `json:"dropped,omitempty"`
}

// Snapshot 60min 一次（唤醒后先发快照再发增量）。
type Snapshot struct {
	Node     string            `json:"node"`
	TS       int64             `json:"ts"`
	Versions Versions          `json:"versions"`
	Sessions []Session         `json:"sessions"`
	Caps     []string          `json:"caps"`
	DiskUsed float64           `json:"disk_used_gb"`
	Extra    map[string]string `json:"extra,omitempty"`
}

type Versions struct {
	Agent  string            `json:"agent"`
	Orca   string            `json:"orca,omitempty"`
	Skills map[string]string `json:"skills,omitempty"`
}

type Session struct {
	ID       string `json:"id"`
	Task     string `json:"task,omitempty"`
	State    string `json:"state"`
	Worktree string `json:"worktree,omitempty"`
}
