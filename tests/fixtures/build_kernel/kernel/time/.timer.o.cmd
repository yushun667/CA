cmd_kernel/time/timer.o := gcc -Wp,-MMD,kernel/time/.timer.o.d -nostdinc -I./include \
    -DKBUILD_MODNAME='"timer"' -include include/linux/kconfig.h -c -o kernel/time/timer.o kernel/time/timer.c
deps_kernel/time/timer.o := \
  kernel/time/timer.c
