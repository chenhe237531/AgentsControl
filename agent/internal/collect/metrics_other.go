//go:build !darwin && !linux

package collect

// defaultMetrics 其余平台（windows 等）：W05 优先交付 macOS/Linux，
// Windows 指标采集留待后续版本，全部返回 -1（服务端不做容量假设）。
func defaultMetrics(_ string) func() Metrics {
	return func() Metrics { return UnknownMetrics() }
}
