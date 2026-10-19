package collect

import (
	"testing"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

func TestHeartbeatUsesInjectedMetrics(t *testing.T) {
	c := New("n-test", "0.1.0")
	c.SetMetricsFn(func() Metrics {
		return Metrics{Load: 1.5, Mem: 0.55, Disk: 0.42, Battery: 87}
	})
	hb := c.Heartbeat(7, 2, 3)
	if hb.Node != "n-test" || hb.AgentVer != "0.1.0" || hb.Seq != 7 {
		t.Fatalf("字段错误: %+v", hb)
	}
	if hb.Load != 1.5 || hb.Mem != 0.55 || hb.Disk != 0.42 || hb.Battery != 87 {
		t.Fatalf("指标未注入: %+v", hb)
	}
	if hb.Sessions != 2 || hb.Queue != 3 {
		t.Fatalf("会话/队列错误: %+v", hb)
	}
}

func TestHeartbeatUnknownMetricsAreMinusOne(t *testing.T) {
	c := New("n-test", "0.1.0")
	c.SetMetricsFn(func() Metrics { return UnknownMetrics() })
	hb := c.Heartbeat(1, 0, 0)
	if hb.Load != -1 || hb.Mem != -1 || hb.Disk != -1 || hb.Battery != -1 {
		t.Fatalf("未知指标必须是 -1，实际 %+v", hb)
	}
}

func TestPanicInMetricsDoesNotKillCollector(t *testing.T) {
	c := New("n-test", "0.1.0")
	c.SetMetricsFn(func() Metrics { panic("probe exploded") })
	hb := c.Heartbeat(1, 0, 0) // 不应 panic
	if hb.Load != -1 || hb.Mem != -1 || hb.Disk != -1 || hb.Battery != -1 {
		t.Fatalf("panic 后应降级为 -1，实际 %+v", hb)
	}
}

func TestSnapshot(t *testing.T) {
	c := New("n-test", "0.2.0")
	snap := c.Snapshot([]protocol.Session{{ID: "s1", State: "running"}}, "1.0.0", 12.5)
	if snap.Node != "n-test" || snap.Versions.Agent != "0.2.0" || snap.Versions.Orca != "1.0.0" {
		t.Fatalf("版本字段错误: %+v", snap)
	}
	if len(snap.Sessions) != 1 || snap.Sessions[0].ID != "s1" {
		t.Fatalf("会话清单错误: %+v", snap.Sessions)
	}
	if snap.DiskUsed != 12.5 {
		t.Fatalf("disk_used 错误: %v", snap.DiskUsed)
	}
	if len(c.Caps()) == 0 {
		t.Fatal("能力标签不应为空")
	}
}

// ---- 解析函数：全部用假数据，不依赖真实硬件 ----

func TestParseDarwinLoadavg(t *testing.T) {
	cases := []struct {
		in   string
		want float64
	}{
		{"{ 1.92 2.13 2.50 }", 1.92},
		{"{ 0.00 0.00 0.00 }", 0},
		{"garbage", -1},
		{"{}", -1},
		{"{ abc 2.13 2.50 }", -1},
	}
	for _, tc := range cases {
		if got := parseDarwinLoadavg(tc.in); got != tc.want {
			t.Errorf("parseDarwinLoadavg(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}

func TestParseDarwinVMStat(t *testing.T) {
	vmstat := `Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               100000.
Pages active:                            1500000.
Pages inactive:                            500000.
Pages speculative:                        100000.
Pages throttled:                              0.
Pages wired down:                          400000.
"Pages occupied by compressor":           100000.
Pages converted into compressor:               0.
Physical pages:                          4000000.
`
	// free = 100000 + 500000 + 100000 = 700000；used = 3300000/4000000 = 0.825
	got := parseDarwinVMStat(vmstat)
	if got < 0.8249 || got > 0.8251 {
		t.Fatalf("vm_stat 解析 = %v, want ≈0.825", got)
	}
	if parseDarwinVMStat("no such header") != -1 {
		t.Fatal("缺 Physical pages 必须返回 -1")
	}
}

func TestParseDarwinBattery(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want float64
	}{
		{"放电中", "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=5908035)	87%; discharging; 0:42 remaining present: true", 87},
		{"充电中", "-InternalBattery-0 (id=5908035)	100%; charged; 0:00 remaining present: true", 100},
		{"台式机", "No batteries are available.", -1},
		{"垃圾输入", "hello world", -1},
	}
	for _, tc := range cases {
		if got := parseDarwinBattery(tc.in); got != tc.want {
			t.Errorf("%s: parseDarwinBattery = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestParseLinuxLoadavg(t *testing.T) {
	if got := parseLinuxLoadavg("0.52 0.58 0.59 1/812 12345"); got != 0.52 {
		t.Fatalf("loadavg = %v, want 0.52", got)
	}
	if parseLinuxLoadavg("") != -1 {
		t.Fatal("空输入必须 -1")
	}
	if parseLinuxLoadavg("abc def") != -1 {
		t.Fatal("非数字必须 -1")
	}
}

func TestParseLinuxMeminfo(t *testing.T) {
	meminfo := `MemTotal:       16000000 kB
MemFree:          100000 kB
MemAvailable:    6000000 kB
Buffers:          200000 kB
Cached:          5000000 kB
`
	// (16000000-6000000)/16000000 = 0.625
	got := parseLinuxMeminfo(meminfo)
	if got < 0.6249 || got > 0.6251 {
		t.Fatalf("meminfo 解析 = %v, want ≈0.625", got)
	}
	if parseLinuxMeminfo("garbage") != -1 {
		t.Fatal("缺 MemTotal 必须 -1")
	}
}
