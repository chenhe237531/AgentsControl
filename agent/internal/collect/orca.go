package collect

import (
	"context"
	"os/exec"
	"strings"
	"time"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

// OrcaProbe 探测 Orca CLI 是否可用并抓取会话清单。
//
// Orca 是可选依赖——探测不到（CLI 缺失/慢响应）时返回空切片，不报错。
// 这条规则硬约束：不要因 Orca 可用性影响设备在线。
type OrcaProbe struct {
	timeout time.Duration
}

func NewOrcaProbe() *OrcaProbe {
	return &OrcaProbe{timeout: 3 * time.Second}
}

// Sessions 返回当前 Orca 会话清单；CLI 不存在或失败返回空切片 + nil。
func (p *OrcaProbe) Sessions(ctx context.Context) []protocol.Session {
	ctx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()

	if _, err := exec.LookPath("orca"); err != nil {
		return nil
	}
	// orca 的 CLI 接口与字段是 W06 的契约，这里只做最小假设：
	// 假设 `orca sessions list --json` 输出每行 "id|state|task|worktree"。
	// 探测不到 orca 或输出格式不符则静默返回空。
	out, err := exec.CommandContext(ctx, "orca", "sessions", "list", "--json").Output()
	if err != nil {
		return nil
	}
	return parseOrcaSessions(string(out))
}

// parseOrcaSessions 容错解析：每行一条记录，字段以 | 分隔；解析不通过整行跳过。
// 暴露给 main 包测试用。
func parseOrcaSessions(s string) []protocol.Session {
	return ParseOrcaSessionsFromTest(s)
}

// ParseOrcaSessionsFromTest 是 parseOrcaSessions 的可导出别名，仅用于跨包测试。
func ParseOrcaSessionsFromTest(s string) []protocol.Session {
	var out []protocol.Session
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || !strings.Contains(line, "|") {
			continue
		}
		// 兼容两种格式：纯 ID 一行，或 "id|state|task|worktier"
		parts := strings.SplitN(line, "|", 4)
		var sess protocol.Session
		sess.ID = parts[0]
		sess.State = "running"
		if len(parts) > 1 && parts[1] != "" {
			sess.State = parts[1]
		}
		if len(parts) > 2 {
			sess.Task = parts[2]
		}
		if len(parts) > 3 {
			sess.Worktree = parts[3]
		}
		if sess.ID == "" {
			continue
		}
		out = append(out, sess)
	}
	return out
}

// OrcaVersion 探测 Orca 版本号；CLI 不可用返回空字符串。
func (p *OrcaProbe) Version(ctx context.Context) string {
	ctx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()
	if _, err := exec.LookPath("orca"); err != nil {
		return ""
	}
	out, err := exec.CommandContext(ctx, "orca", "--version").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}