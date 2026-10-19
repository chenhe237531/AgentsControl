// Package main · install 子命令。
//
// `afc-agent install` 一条命令完成服务注册与开机自启；
// `afc-agent uninstall` 卸载干净。
//
// 平台实现：
//   - darwin: LaunchAgent（~/Library/LaunchAgents/）
//   - linux:  systemd --user 单元（~/.config/systemd/user/）
//   - windows: Winsw 服务（注册为系统服务）
//
// 失败时只输出一句人话 + 日志路径——不让用户读 stack trace。
// 实现"不让用户读 stack trace"的方式：runInstall/runUninstall 返回 error，
// main 把 error 转成一行业话 + 日志路径输出后以非 0 退出。

package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
)

const (
	// 服务名同 Linux/macOS 用户约定、Windows 服务名独立
	ServiceName = "dev.afc.agent"
	// LaunchAgent Label / systemd Unit 名 / plist 文件名的前缀
	ServiceUnit = "dev.afc.agent"
)

// installCmd 解析 install 子命令参数。
// 需要把运行时环境变量写进服务定义，避免"装好了但起不来"。
type installCmd struct {
	binary    string
	envFile   string
	extraArgs []string
}

func (c *installCmd) parse(args []string) error {
	fs := flag.NewFlagSet("install", flag.ContinueOnError)
	fs.StringVar(&c.binary, "binary", "", "afc-agent 可执行路径（默认用 os.Executable）")
	fs.StringVar(&c.envFile, "env", "", "环境变量文件路径（KEY=VALUE 一行一对，写进服务定义）")
	if err := fs.Parse(args); err != nil {
		return err
	}
	c.extraArgs = fs.Args()
	return nil
}

func runInstall(args []string) int {
	var c installCmd
	if err := c.parse(args); err != nil {
		fmt.Fprintf(os.Stderr, "install: %v\n", err)
		return 2
	}
	bin := c.binary
	if bin == "" {
		exe, err := os.Executable()
		if err != nil {
			fmt.Fprintf(os.Stderr, "无法确定可执行路径：%v（用 --binary 显式指定）\n", err)
			return 1
		}
		// 解析符号链接拿到真实路径（brew bin 等）；失败时保留原路径继续
		if resolved, err := filepath.EvalSymlinks(exe); err == nil {
			exe = resolved
		}
		bin = exe
	}
	if _, err := os.Stat(bin); err != nil {
		fmt.Fprintf(os.Stderr, "找不到可执行文件 %s：%v\n", bin, err)
		return 1
	}

	env, err := loadEnvFile(c.envFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取环境变量文件失败：%v\n", err)
		return 1
	}
	if env["AFC_STATE_PATH"] == "" {
		// 默认放统一位置，便于卸载时清理
		home, _ := os.UserHomeDir()
		if home != "" {
			env["AFC_STATE_PATH"] = filepath.Join(home, ".afc-agent")
		}
	}

	if err := platformInstall(bin, env, c.extraArgs); err != nil {
		fmt.Fprintf(os.Stderr, "安装失败：%v\n", err)
		fmt.Fprintf(os.Stderr, "排查请看日志：%s\n", logPath())
		slog.Error("install failed", "err", err)
		return 1
	}
	fmt.Println("已安装并启用，下次登录/开机自启。状态：")
	fmt.Printf("  - 启动: %s\n", startHint())
	fmt.Printf("  - 状态/日志: %s\n", logPath())
	return 0
}

func runUninstall() int {
	if err := platformUninstall(); err != nil {
		fmt.Fprintf(os.Stderr, "卸载失败：%v\n", err)
		return 1
	}
	fmt.Println("已卸载，残留文件已清理。")
	return 0
}

// loadEnvFile 读 KEY=VALUE 文件。# 开头注释；空行跳过。
// 不解析引号——设备端配置应该简单到不用学。
func loadEnvFile(path string) (map[string]string, error) {
	env := map[string]string{}
	if path == "" {
		return env, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || line[0] == '#' {
			continue
		}
		eq := strings.IndexByte(line, '=')
		if eq < 0 {
			continue
		}
		env[strings.TrimSpace(line[:eq])] = strings.TrimSpace(line[eq+1:])
	}
	return env, nil
}

// logPath 各平台实现不同（macOS 走 unifiedlog，linux 走 journald，
// windows 走事件日志）；这里返回一个用户能 cat 的路径。
func logPath() string {
	return platformLogPath()
}
