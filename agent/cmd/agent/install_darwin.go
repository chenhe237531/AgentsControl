//go:build darwin

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// platformInstall macOS：写 ~/Library/LaunchAgents/dev.afc.agent.plist，然后 launchctl load。
// KeepAlive=true 保证崩溃自动重启；RunAtLoad 保证登录即起。
func platformInstall(bin string, env map[string]string, extraArgs []string) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("取 home: %w", err)
	}
	label := ServiceUnit
	plistPath := filepath.Join(home, "Library", "LaunchAgents", label+".plist")
	if err := os.MkdirAll(filepath.Dir(plistPath), 0o755); err != nil {
		return fmt.Errorf("创建 LaunchAgents 目录: %w", err)
	}
	envStrings := make([]string, 0, len(env))
	for k, v := range env {
		envStrings = append(envStrings, fmt.Sprintf(`<string>%s=%s</string>`, k, v))
	}
	args := []string{bin}
	args = append(args, extraArgs...)
	argStrings := make([]string, 0, len(args))
	for _, a := range args {
		argStrings = append(argStrings, fmt.Sprintf(`<string>%s</string>`, a))
	}
	plist := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%s</string>
    <key>ProgramArguments</key>
    <array>
%s
    </array>
    <key>EnvironmentVariables</key>
    <dict>
%s
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>%s/log/afc-agent.out.log</string>
    <key>StandardErrorPath</key><string>%s/log/afc-agent.err.log</string>
</dict>
</plist>
`, label, strings.Join(argStrings, "\n        "), strings.Join(envStrings, "\n        "),
		env["AFC_STATE_PATH"], env["AFC_STATE_PATH"])
	if err := os.WriteFile(plistPath, []byte(plist), 0o644); err != nil {
		return fmt.Errorf("写 plist: %w", err)
	}
	// 若已加载则先 unload，避免 "service already loaded"
	_ = exec.Command("launchctl", "unload", plistPath).Run()
	if err := exec.Command("launchctl", "load", plistPath).Run(); err != nil {
		return fmt.Errorf("launchctl load: %w（plist 已写入 %s，可手动 load 排查）", err, plistPath)
	}
	return nil
}

func platformUninstall() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	label := ServiceUnit
	plistPath := filepath.Join(home, "Library", "LaunchAgents", label+".plist")
	if _, err := os.Stat(plistPath); os.IsNotExist(err) {
		return fmt.Errorf("未安装（plist 不存在：%s）", plistPath)
	}
	_ = exec.Command("launchctl", "unload", plistPath).Run()
	if err := os.Remove(plistPath); err != nil {
		return fmt.Errorf("删 plist: %w", err)
	}
	return nil
}

func platformLogPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".afc-agent", "log", "afc-agent.err.log")
}

func startHint() string {
	return "launchctl list | grep " + ServiceUnit
}