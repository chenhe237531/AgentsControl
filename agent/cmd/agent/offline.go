// Package main · 离线降级辅助。
//
// 当 buffer.Append 返回 ErrDiskPressure 时：
//   - 不再尝试写缓冲（水位 > 90%，再写会拖垮系统）
//   - 记一行明显日志（让运维知道是磁盘问题而不是网络问题）
//   - 由服务端通过"长时间无心跳/事件"自行判 offline（设备端不假装在线）
package main

import (
	"log/slog"

	"github.com/jackma3327/AgentsControl/agent/internal/buffer"
	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

// appendEvent 把执行面产生的事件写入本地缓冲。
// 在 disk_pressure 时只记日志不写入；返回 ok=false 让调用方决定是否丢弃。
// 调用方（W06 执行面）应把 disk_pressure 作为本地失败上报给任务状态机。
func appendEvent(buf buffer.Buffer, ev protocol.Event) (ok bool) {
	_, _, err := buf.Append(ev)
	if err == nil {
		return true
	}
	if err == buffer.ErrDiskPressure {
		slog.Error("磁盘水位 > 90%，拒绝写入缓冲并停报事件",
			"kind", ev.Kind, "task", ev.Task, "err", err)
		return false
	}
	slog.Warn("buffer append 失败", "err", err)
	return false
}
