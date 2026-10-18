package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadEnvFile(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, ".env")
	content := `# comment line
AFC_API_BASE_URL=http://127.0.0.1:8000/api/v1
AFC_NODE_ID=n-test
AFC_TOKEN=secret

  # leading-space comment
`
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	env, err := loadEnvFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if env["AFC_API_BASE_URL"] != "http://127.0.0.1:8000/api/v1" {
		t.Errorf("API URL not loaded: %v", env)
	}
	if env["AFC_NODE_ID"] != "n-test" {
		t.Errorf("Node ID not loaded: %v", env)
	}
	if env["AFC_TOKEN"] != "secret" {
		t.Errorf("Token not loaded: %v", env)
	}
}

func TestLoadEnvFileMissingIsOK(t *testing.T) {
	env, err := loadEnvFile("")
	if err != nil || env == nil {
		t.Fatalf("空路径应返回空 map + nil err，实得 %v %v", env, err)
	}
}

func TestLoadEnvFileNotExistsIsError(t *testing.T) {
	if _, err := loadEnvFile("/nonexistent/path/.env"); err == nil {
		t.Fatal("文件不存在应返回 error")
	}
}

func TestInstallCmdParse(t *testing.T) {
	var c installCmd
	if err := c.parse([]string{"--binary", "/x/afc-agent", "--env", "/etc/afc.env", "extra-arg"}); err != nil {
		t.Fatalf("parse: %v", err)
	}
	if c.binary != "/x/afc-agent" || c.envFile != "/etc/afc.env" {
		t.Fatalf("字段错误: %+v", c)
	}
	if len(c.extraArgs) != 1 || c.extraArgs[0] != "extra-arg" {
		t.Fatalf("extra args 未保留: %+v", c.extraArgs)
	}
}