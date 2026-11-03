# 只编译 gen2kr
# （gen2kp.c / gen2hw.c 是早期实验版本，放在 legacy/ 里，不参与编译）
obj-m := gen2kr.o

KDIR := /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

.PHONY: all clean
