// Package config 载入设备端配置。
//
// 设计取舍：设备端不允许"缺配置就跑"。注册产生的 token 缺失时直接退出，
// 避免出现"看起来在跑、其实一直在 401"的假在线设备（这类故障最难排查）。
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"time"
)

type Config struct {
	APIBaseURL string
	NodeID     string
	Token      string
	AgentVer   string

	HeartbeatInterval time.Duration
	FlushInterval     time.Duration
	SnapshotInterval  time.Duration

	// BufferKind: "sqlite"（默认，断电不丢）或 "memory"（仅测试用）
	BufferKind string
	// BufferPath: SQLite 文件路径；空则用 StatePath/buffer.db
	BufferPath     string
	BufferMaxBytes int64
	BufferMaxAge   time.Duration

	StatePath string
}

func Load() (*Config, error) {
	c := &Config{
		APIBaseURL:        os.Getenv("AFC_API_BASE_URL"),
		NodeID:            os.Getenv("AFC_NODE_ID"),
		Token:             os.Getenv("AFC_TOKEN"),
		AgentVer:          envOr("AFC_AGENT_VERSION", "0.1.0"),
		HeartbeatInterval: durationEnv("AFC_HEARTBEAT_SECONDS", 60*time.Second),
		FlushInterval:     durationEnv("AFC_FLUSH_SECONDS", 30*time.Second),
		SnapshotInterval:  durationEnv("AFC_SNAPSHOT_SECONDS", 60*time.Minute),
		BufferKind:        envOr("AFC_BUFFER_KIND", "sqlite"),
		BufferPath:        os.Getenv("AFC_BUFFER_PATH"),
		BufferMaxBytes:    int64Env("AFC_BUFFER_MAX_MB", 100) * 1024 * 1024,
		BufferMaxAge:      7 * 24 * time.Hour,
		StatePath:         envOr("AFC_STATE_PATH", defaultStatePath()),
	}

	var missing []string
	if c.APIBaseURL == "" {
		missing = append(missing, "AFC_API_BASE_URL")
	}
	if c.Token == "" {
		missing = append(missing, "AFC_TOKEN")
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("缺少必需配置: %v", missing)
	}

	// 心跳必须显著快于"离线判定"（服务端默认 300s），否则永远判不出 offline
	if c.HeartbeatInterval >= 3*time.Minute {
		return nil, errors.New("AFC_HEARTBEAT_SECONDS 必须 < 180s")
	}
	if c.FlushInterval <= 0 || c.SnapshotInterval <= 0 {
		return nil, errors.New("间隔必须是正数")
	}
	switch c.BufferKind {
	case "sqlite", "memory":
	default:
		return nil, fmt.Errorf("AFC_BUFFER_KIND 非法: %s（允许 sqlite/memory）", c.BufferKind)
	}
	return c, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func durationEnv(key string, fallback time.Duration) time.Duration {
	raw := os.Getenv(key)
	if raw == "" {
		return fallback
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 {
		return fallback
	}
	return time.Duration(n) * time.Second
}

func int64Env(key string, fallback int64) int64 {
	raw := os.Getenv(key)
	if raw == "" {
		return fallback
	}
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func defaultStatePath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return "./afc-agent-state"
	}
	return home + "/.afc-agent"
}
