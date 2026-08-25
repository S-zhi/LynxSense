package drive

import "testing"

// TestParseDriveRange 验证 Drive Range 响应头到下一个偏移量的解析结果。
func TestParseDriveRange(t *testing.T) {
	t.Parallel()
	tests := []struct {
		input string
		want  int64
	}{
		{input: "bytes=0-0", want: 1},
		{input: "bytes=0-16777215", want: 16777216},
		{input: "", want: 0},
		{input: "invalid", want: 0},
		{input: "bytes=1-nope", want: 0},
	}
	for _, test := range tests {
		if got := parseDriveRange(test.input); got != test.want {
			t.Errorf("parseDriveRange(%q) = %d, want %d", test.input, got, test.want)
		}
	}
}

// TestEscapeQueryLiteral 验证 Drive 查询字面量中的单引号转义。
func TestEscapeQueryLiteral(t *testing.T) {
	if got := escapeQueryLiteral("a'b"); got != "a\\'b" {
		t.Fatalf("escaped query literal = %q", got)
	}
}
