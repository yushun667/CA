# build fixture: two real C translation units compiled with the host compiler.
# Used by tests/test_build_capture.py to prove capture_build runs a real make.
CC ?= cc
CFLAGS ?= -O2 -g -Iinclude
TARGETS = hello.o util.o

all: $(TARGETS)

hello.o: hello.c include/greeting.h
	$(CC) $(CFLAGS) -c -o $@ $<

util.o: util.c include/greeting.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f $(TARGETS)

.PHONY: all clean
