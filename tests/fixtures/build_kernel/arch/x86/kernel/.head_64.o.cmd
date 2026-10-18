cmd_arch/x86/kernel/head_64.o := gcc -Wp,-MMD,arch/x86/kernel/.head_64.o.d -nostdinc -I./arch/x86/include -c -o arch/x86/kernel/head_64.o arch/x86/kernel/head_64.S
deps_arch/x86/kernel/head_64.o := \
  arch/x86/kernel/head_64.S \
  include/linux/kconfig.h
