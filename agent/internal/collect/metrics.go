package collect

import "os"

// Metrics 平台指标值。任何字段"采集不到"都用 -1 表示未知，
// 不要用 0——0 会被服务端当成"空闲/满电/无负载"参与调度假设。
type Metrics struct {
	Load    float64 // 1min load average（darwin 无量纲；linux 与核数同量纲）
	Mem     float64 // 内存已用比例 0..1
	Disk    float64 // 磁盘已用比例 0..1
	Battery float64 // 电量百分比 0..100；台式机/未知 -1
}

// UnknownMetrics 返回全 -1 的指标。
func UnknownMetrics() Metrics {
	return Metrics{Load: -1, Mem: -1, Disk: -1, Battery: -1}
}

// homeDir 尽力取用户主目录，失败则用 "."（statfs 对 "." 也能工作）。
func homeDir() string {
	if h, err := os.UserHomeDir(); err == nil && h != "" {
		return h
	}
	return "."
}
