cmd_kernel/sched/core.o := gcc -Wp,-MMD,kernel/sched/.core.o.d -nostdinc -isystem /usr/lib/gcc/x86_64-linux-gnu/13/include -I./arch/x86/include -I./include -D__KERNEL__ -DKBUILD_MODNAME='"sched"' -include include/linux/compiler-version.h -include include/linux/kconfig.h -c -o kernel/sched/core.o kernel/sched/core.c
deps_kernel/sched/core.o := \
  kernel/sched/core.c \
  include/linux/sched.h \
  include/linux/kconfig.h
