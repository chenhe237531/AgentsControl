// Package collect 采集心跳与能力标签。
//
// 骨架只做跨平台安全的部分（内存、goroutine、能力探测）；
// load/disk/battery 需要平台特化实现，属于 W05 的 I01/I02。
package collect

import (
	"os/exec"
	"runtime"
	"time"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

type Collector struct {
	NodeID   string
	AgentVer string
	started  time.Time
	caps     []string
}

func New(nodeID, agentVer string) *Collector {
	c := &Collector{NodeID: nodeID, AgentVer: agentVer, started: time.Now()}
	c.caps = detectCaps()
	return c
}

func (c *Collector) Caps() []string { return c.caps }

// Heartbeat 组装一次心跳。未知指标用 -1 表示"未知"，服务端据此不做容量假设。
func (c *Collector) Heartbeat(seq uint64, sessions, queue int) protocol.Heartbeat {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)

	return protocol.Heartbeat{
		Node:     c.NodeID,
		TS:       time.Now().Unix(),
		Seq:      seq,
		Load:     -1, // TODO(W05): 平台特化（darwin: host_statistics / linux: /proc/loadavg）
		Mem:      ratio(ms.Sys, ms.Sys+1),
		Disk:     -1,
		Battery:  -1,
		Sessions: sessions,
		Queue:    queue,
		AgentVer: c.AgentVer,
	}
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

func ratio(a, b uint64) float64 {
	if b == 0 {
		return 0
	}
	return float64(a) / float64(b)
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
