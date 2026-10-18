//go:build linux

package collect

import (
	"os"
	"strconv"
	"strings"
	"syscall"
)

// defaultMetrics Linux 实现：
//   - load：/proc/loadavg 第一列
//   - mem：/proc/meminfo（MemTotal - MemAvailable）/ MemTotal
//   - disk：statfs 观测路径所在卷
//   - battery：/sys/class/power_supply/BAT*/capacity；没有电池（台式机/VM）返回 -1
func defaultMetrics(diskPath string) func() Metrics {
	return func() Metrics {
		return Metrics{
			Load:    linuxLoad(),
			Mem:     linuxMem(),
			Disk:    diskUsedRatio(diskPath),
			Battery: linuxBattery(),
		}
	}
}

func linuxLoad() float64 {
	b, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return -1
	}
	return parseLinuxLoadavg(string(b))
}

func linuxMem() float64 {
	b, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return -1
	}
	return parseLinuxMeminfo(string(b))
}

func linuxBattery() float64 {
	entries, err := os.ReadDir("/sys/class/power_supply")
	if err != nil {
		return -1
	}
	for _, e := range entries {
		if !strings.HasPrefix(e.Name(), "BAT") {
			continue
		}
		b, err := os.ReadFile("/sys/class/power_supply/" + e.Name() + "/capacity")
		if err != nil {
			continue
		}
		if n, err := strconv.ParseFloat(strings.TrimSpace(string(b)), 64); err == nil {
			return n
		}
	}
	return -1
}

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
