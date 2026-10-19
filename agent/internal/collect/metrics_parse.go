// 纯解析函数：输入是字符串（命令输出/文件内容的假数据），无副作用，
// 不加 build tag，使单测在任意平台都能跑（CI 在 linux，开发机在 macOS）。

package collect

import (
	"strconv"
	"strings"
)

// parseDarwinLoadavg 解析 `sysctl -n vm.loadavg` 输出 "{ 1.92 2.13 2.50 }" 取 1min。
func parseDarwinLoadavg(s string) float64 {
	s = strings.TrimSpace(s)
	s = strings.TrimSuffix(strings.TrimPrefix(s, "{"), "}")
	fields := strings.Fields(s)
	if len(fields) == 0 {
		return -1
	}
	v, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return -1
	}
	return v
}

// parseDarwinVMStat 解析 vm_stat 输出，返回内存已用比例。
// used ≈ total - (free + inactive + speculative)；解析不了返回 -1。
func parseDarwinVMStat(s string) float64 {
	pages := map[string]uint64{}
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "Page size:") {
			// 页大小本可读出，但 used/total 用页数比即可约掉，此处不依赖它。
			continue
		}
		idx := strings.IndexByte(line, ':')
		if idx < 0 {
			continue
		}
		key := strings.TrimSpace(line[:idx])
		val := strings.TrimSuffix(strings.TrimSpace(line[idx+1:]), ".")
		n, err := strconv.ParseUint(val, 10, 64)
		if err != nil {
			continue
		}
		pages[key] = n
	}
	total := pages["Physical pages"]
	if total == 0 {
		return -1
	}
	free := pages["Pages free"] + pages["Pages inactive"] + pages["Pages speculative"]
	if free > total {
		return -1
	}
	return float64(total-free) / float64(total)
}

// parseDarwinBattery 解析 `pmset -g batt` 的电量百分比。
// 无电池（"No batteries"）或解析失败返回 -1。
func parseDarwinBattery(s string) float64 {
	if strings.Contains(s, "No batteries") || strings.Contains(s, "no batteries") {
		return -1
	}
	// 形如 "InternalBattery-0 (id= ...)	87%; discharging; 0:42 remaining present: true"
	for _, line := range strings.Split(s, "\n") {
		pct := -1
		fields := strings.FieldsFunc(line, func(r rune) bool {
			return r == ' ' || r == '\t' || r == ';'
		})
		for _, f := range fields {
			if strings.HasSuffix(f, "%") {
				if n, err := strconv.Atoi(strings.TrimSuffix(f, "%")); err == nil {
					pct = n
				}
			}
		}
		if pct >= 0 {
			return float64(pct)
		}
	}
	return -1
}

// parseLinuxLoadavg 解析 /proc/loadavg 第一列。
func parseLinuxLoadavg(s string) float64 {
	fields := strings.Fields(s)
	if len(fields) == 0 {
		return -1
	}
	v, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return -1
	}
	return v
}

// parseLinuxMeminfo 解析 /proc/meminfo（kB），返回 (MemTotal-MemAvailable)/MemTotal。
func parseLinuxMeminfo(s string) float64 {
	var total, avail uint64
	for _, line := range strings.Split(s, "\n") {
		idx := strings.IndexByte(line, ':')
		if idx < 0 {
			continue
		}
		key := line[:idx]
		fields := strings.Fields(line[idx+1:])
		if len(fields) == 0 {
			continue
		}
		n, err := strconv.ParseUint(fields[0], 10, 64)
		if err != nil {
			continue
		}
		switch key {
		case "MemTotal":
			total = n
		case "MemAvailable":
			avail = n
		}
	}
	if total == 0 || avail > total {
		return -1
	}
	return float64(total-avail) / float64(total)
}
