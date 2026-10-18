//go:build linux

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// platformInstall linux：写 ~/.config/systemd/user/，systemctl --user enable --now。
// Restart=always 保证崩溃自动重启。
func platformInstall(bin string, env map[string]string, extraArgs []string) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("取 home: %w", err)
	}
	unit := ServiceUnit + ".service"
	unitPath := filepath.Join(home, ".config", "systemd", "user", unit)
	if err := os.MkdirAll(filepath.Dir(unitPath), 0o755); err != nil {
		return fmt.Errorf("创建 systemd user 目录: %w", err)
	}
	envLines := make([]string, 0, len(env))
	for k, v := range env {
		envLines = append(envLines, fmt.Sprintf("Environment=%s=%s", k, v))
	}
	args := append([]string{bin}, extraArgs...)
	execStart := strings.Join(args, " ")
	content := fmt.Sprintf(`[Unit]
Description=AFC Agent (device side)
After=network-online.target

[Service]
ExecStart=%s
%s
Restart=always
RestartSec=5
StandardOutput=append:%s/log/afc-agent.out.log
StandardError=append:%s/log/afc-agent.err.log

[Install]
WantedBy=default.target
`, execStart, strings.Join(envLines, "\n"),
		env["AFC_STATE_PATH"], env["AFC_STATE_PATH"])
	if err := os.WriteFile(unitPath, []byte(content), 0o644); err != nil {
		return fmt.Errorf("写 unit: %w", err)
	}
	if err := exec.Command("systemctl", "--user", "daemon-reload").Run(); err != nil {
		return fmt.Errorf("daemon-reload: %w", err)
	}
	if err := exec.Command("systemctl", "--user", "enable", "--now", unit).Run(); err != nil {
		return fmt.Errorf("enable --now: %w（unit 已写入 %s）", err, unitPath)
	}
	return nil
}

func platformUninstall() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	unit := ServiceUnit + ".service"
	unitPath := filepath.Join(home, ".config", "systemd", "user", unit)
	if _, err := os.Stat(unitPath); os.IsNotExist(err) {
		return fmt.Errorf("未安装（unit 不存在：%s）", unitPath)
	}
	_ = exec.Command("systemctl", "--user", "disable", "--now", unit).Run()
	if err := os.Remove(unitPath); err != nil {
		return fmt.Errorf("删 unit: %w", err)
	}
	_ = exec.Command("systemctl", "--user", "daemon-reload").Run()
	return nil
}

func platformLogPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".afc-agent", "log", "afc-agent.err.log")
}

func startHint() string {
	return "systemctl --user status " + ServiceUnit
}
