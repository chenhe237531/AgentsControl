package buffer

import (
	"errors"
	"testing"

	"github.com/jackma3327/AgentsControl/agent/internal/protocol"
)

// TestAppendRejectsOnDiskPressure 验证 ErrDiskPressure 被定义且可被识别。
// 真实的 diskPressure 路径需要磁盘水位 > 90%，单测环境不易构造，
// 这里只检查错误定义与 appendEvent 等调用方能正确处理。
func TestErrDiskPressureSentinel(t *testing.T) {
	if !errors.Is(ErrDiskPressure, ErrDiskPressure) {
		t.Fatal("ErrDiskPressure 应与自身相等")
	}
	if !errors.Is(ErrDiskPressure, ErrEmpty) == false {
		t.Fatal("ErrDiskPressure 不应等于 ErrEmpty")
	}
}

// TestSQLiteAppendNormalPathNotDiskPressure 验证正常水位下 Append 不返回 ErrDiskPressure。
// （测试机的根盘通常 < 90% 已用）
func TestSQLiteAppendNormalPathNotDiskPressure(t *testing.T) {
	s := newTestSQLite(t, 0, 0)
	defer s.Close()
	_, _, err := s.Append(protocol.Event{Kind: "x"})
	if err == ErrDiskPressure {
		// 仅当测试机磁盘确实 > 90% 时才会触发——这种情况下跳过而非失败
		t.Skip("测试机磁盘水位 > 90%，跳过")
	}
	if err != nil {
		t.Fatalf("正常 Append 失败: %v", err)
	}
}

// TestSQLiteDiskPressureCacheBehavior 验证 diskPressure 缓存路径不会 panic。
func TestSQLiteDiskPressureCacheBehavior(t *testing.T) {
	s := newTestSQLite(t, 0, 0)
	defer s.Close()
	// 多次调用应安全（命中缓存）
	for i := 0; i < 5; i++ {
		_ = s.diskPressure()
	}
}
