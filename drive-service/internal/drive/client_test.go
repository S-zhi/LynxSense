package drive

import "testing"

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

func TestEscapeQueryLiteral(t *testing.T) {
	if got := escapeQueryLiteral("a'b"); got != "a\\'b" {
		t.Fatalf("escaped query literal = %q", got)
	}
}
