//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// platformInstall windows：用 sc.exe 注册系统服务，配 Recovery 动作实现崩溃重启。
// （Winsw 需附带 winsw.exe，对零依赖单二进制目标更重；sc.exe 是系统自带，先选它。）
func platformInstall(bin string, env map[string]string, extraArgs []string) error {
	name := ServiceName
	// 已存在先停掉删掉，避免 "already exists"
	_ = exec.Command("sc.exe", "stop", name).Run()
	_ = exec.Command("sc.exe", "delete", name).Run()

	if err := exec.Command("sc.exe", "create",
		name,
		"binPath=", fmt.Sprintf(`"%s" %s`, bin, strings.Join(extraArgs, " ")),
		"DisplayName=", "AFC Agent",
		"start=", "auto").Run(); err != nil {
		return fmt.Errorf("sc create: %w", err)
	}
	// 崩溃自动重启：失败后 5s 重启，三次失败升级到 60s
	if err := exec.Command("sc.exe", "failure",
		name, "reset=", "86400",
		"actions=", "restart/5000/restart/5000/restart/60000").Run(); err != nil {
		return fmt.Errorf("sc failure: %w", err)
	}
	// 环境变量写到系统服务（通过注册表会在 service 启动时生效，但 sc.exe 不直接支持；
	// 这里推荐用 .env 文件 + agent 自行读取，避免改动系统注册表）
	if envPath := env["AFC_ENV_FILE"]; envPath != "" {
		fmt.Fprintf(os.Stderr, "warning: Windows 服务不直接读 KEY=VALUE 文件，请用 AFC_ENV_FILE=%s 让 agent 自行读取\n", envPath)
	}
	return exec.Command("sc.exe", "start", name).Run()
}

func platformUninstall() error {
	name := ServiceName
	_ = exec.Command("sc.exe", "stop", name).Run()
	if err := exec.Command("sc.exe", "delete", name).Run(); err != nil {
		return fmt.Errorf("sc delete: %w", err)
	}
	return nil
}

func platformLogPath() string {
	home, _ := os.UserHomeDir()
	return home + "\\.afc-agent\\log\\afc-agent.err.log"
}

func startHint() string {
	return "sc.exe query " + ServiceName
}
