//go:build darwin

package collect

import (
	"os/exec"
	"syscall"
)

// defaultMetrics macOS 实现：
//   - load：sysctl vm.loadavg（输出 "{ 1.92 2.13 2.50 }"）
//   - mem：vm_stat（页计数 + 页大小 → 已用比例）
//   - disk：statfs 观测路径所在卷
//   - battery：pmset -g batt（台式机输出 "No batteries" → -1）
//
// 全部走外部命令 + 解析，避免 CGO；命令失败/解析失败只影响该字段（-1）。
func defaultMetrics(diskPath string) func() Metrics {
	return func() Metrics {
		return Metrics{
			Load:    darwinLoad(),
			Mem:     darwinMem(),
			Disk:    diskUsedRatio(diskPath),
			Battery: darwinBattery(),
		}
	}
}

func darwinLoad() float64 {
	out, err := exec.Command("sysctl", "-n", "vm.loadavg").Output()
	if err != nil {
		return -1
	}
	return parseDarwinLoadavg(string(out))
}

func darwinMem() float64 {
	out, err := exec.Command("vm_stat").Output()
	if err != nil {
		return -1
	}
	return parseDarwinVMStat(string(out))
}

func darwinBattery() float64 {
	out, err := exec.Command("pmset", "-g", "batt").Output()
	if err != nil {
		return -1
	}
	return parseDarwinBattery(string(out))
}

// diskUsedRatio 用 statfs 算已用比例；失败返回 -1。
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
