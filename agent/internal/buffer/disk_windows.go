//go:build windows

package buffer

// diskUsedRatio Windows 实现留待后续；当前返回 -1，
// diskPressure 因 0.90 > -1 不成立而永不触发，安全降级。
func diskUsedRatio(path string) float64 { return -1 }
