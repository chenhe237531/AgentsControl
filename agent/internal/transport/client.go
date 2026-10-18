// Package transport 负责把上报送到控制面。
//
// 三条硬约定：
//  1. 每个请求都签名：HMAC-SHA256(f"{ts}." + body)，时间戳窗口默认 300s；
//  2. 幂等由 (node_id, seq) 保证，因此**失败可以无脑重发**，不会产生重复事件；
//  3. 重试只针对网络错误与 5xx；4xx 一律不重试（token 失效重试也没用，只会刷日志）。
package transport

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"
)

type Client struct {
	baseURL string
	nodeID  string
	secret  string
	hc      *http.Client
}

func New(baseURL, nodeID, secret string) *Client {
	return &Client{
		baseURL: baseURL,
		nodeID:  nodeID,
		secret:  secret,
		hc: &http.Client{
			Timeout: 15 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns:        8,
				MaxIdleConnsPerHost: 4,
				IdleConnTimeout:     90 * time.Second,
			},
		},
	}
}

// StatusError 表示服务端明确拒绝（4xx），不应重试。
type StatusError struct {
	Status int
	Body   string
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("upstream %d: %s", e.Status, e.Body)
}

func (c *Client) Post(ctx context.Context, path string, payload any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}

	var lastErr error
	backoff := 500 * time.Millisecond
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(backoff):
			}
			backoff *= 2
		}

		err := c.once(ctx, path, body)
		if err == nil {
			return nil
		}
		var se *StatusError
		if errors.As(err, &se) {
			return err // 4xx：不重试
		}
		lastErr = err
	}
	return lastErr
}

func (c *Client) once(ctx context.Context, path string, body []byte) error {
	ts := time.Now().Unix()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Afc-Node", c.nodeID)
	req.Header.Set("X-Afc-Timestamp", strconv.FormatInt(ts, 10))
	req.Header.Set("X-Afc-Signature", sign(c.secret, body, ts))

	res, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()

	if res.StatusCode >= 200 && res.StatusCode < 300 {
		_, _ = io.Copy(io.Discard, res.Body)
		return nil
	}

	snippet, _ := io.ReadAll(io.LimitReader(res.Body, 512))
	if res.StatusCode < 500 {
		return &StatusError{Status: res.StatusCode, Body: string(snippet)}
	}
	return fmt.Errorf("upstream %d: %s", res.StatusCode, snippet)
}

func sign(secret string, body []byte, ts int64) string {
	m := hmac.New(sha256.New, []byte(secret))
	m.Write([]byte(strconv.FormatInt(ts, 10)))
	m.Write([]byte("."))
	m.Write(body)
	return hex.EncodeToString(m.Sum(nil))
}
