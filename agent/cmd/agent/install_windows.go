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
	// sc.exe 不支持直接传环境变量；服务环境变量写入注册表
	// HKLM\...\Services\<name>\Environment（REG_MULTI_SZ），服务控制管理器启动时会读取。
	// 之前这里只打一行 warning 然后丢弃全部 env——装出来的服务会因缺 AFC_TOKEN 反复崩溃重启。
	if len(env) > 0 {
		values := make([]string, 0, len(env))
		for k, v := range env {
			values = append(values, k+"="+v)
		}
		// reg.exe 对 REG_MULTI_SZ 以字面 \0 分隔多个值
		if out, err := exec.Command("reg.exe", "add",
			`HKLM\SYSTEM\CurrentControlSet\Services\`+name+`\Environment`,
			"/v", "Environment", "/t", "REG_MULTI_SZ", "/d",
			strings.Join(values, `\0`), "/f").CombinedOutput(); err != nil {
			return fmt.Errorf("reg add Environment: %w（%s）", err, strings.TrimSpace(string(out)))
		}
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
