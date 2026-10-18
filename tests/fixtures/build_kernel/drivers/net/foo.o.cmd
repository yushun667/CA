cmd_drivers/net/foo.o := clang -nostdinc -I./include -DKBUILD_MODNAME='"foo"' -c -o drivers/net/foo.o drivers/net/foo.c
deps_drivers/net/foo.o := \
  drivers/net/foo.c
