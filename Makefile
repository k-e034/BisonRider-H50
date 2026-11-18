# 汲み出しの読み（BAR0 の窓を 16 バイト幅で読む）。無くても動くが、string_at では約 6 Mbps で頭打ちになる
CC ?= gcc
CFLAGS ?= -O2 -mavx2 -shared -fPIC

all: dth50/fastread.so

dth50/fastread.so: dth50/fastread.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f dth50/fastread.so

.PHONY: all clean
