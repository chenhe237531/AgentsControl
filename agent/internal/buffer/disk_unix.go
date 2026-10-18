//go:build darwin || linux

// 跨平台磁盘水位检查（被 SQLite.diskPressure 调用）。
// 与 collect 包的 diskUsedRatio 实现一致但独立，避免 buffer 依赖 collect。

package buffer

import "syscall"

// diskUsedRatio 用 statfs 算已用比例；失败返回 -1。
// 实现 darwin 与 linux 都用（Statfs_t 字段布局兼容；windows 单独实现）。
func diskUsedRatio(path string) float64 {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return -1
	}
	total := uint64(st.Bsize) * uint64(st.Blocks)
	if total == 0 {
		return -1
	}
	used := total - uint64(st.Bsize)*uint64(st.Bfree)
	if used > total {
		return -1
	}
	return float64(used) / float64(total)
}
