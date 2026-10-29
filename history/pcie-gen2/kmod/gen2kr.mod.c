#include <linux/module.h>
#include <linux/export-internal.h>
#include <linux/compiler.h>

MODULE_INFO(name, KBUILD_MODNAME);

__visible struct module __this_module
__section(".gnu.linkonce.this_module") = {
	.name = KBUILD_MODNAME,
	.init = init_module,
#ifdef CONFIG_MODULE_UNLOAD
	.exit = cleanup_module,
#endif
	.arch = MODULE_ARCH_INIT,
};



static const struct modversion_info ____versions[]
__used __section("__versions") = {
	{ 0xf0fdf6cb, "__stack_chk_fail" },
	{ 0xffeedf6a, "delayed_work_timer_fn" },
	{ 0xc6f46339, "init_timer_key" },
	{ 0x92997ed8, "_printk" },
	{ 0x456b7f34, "pci_get_device" },
	{ 0xde80cd09, "ioremap" },
	{ 0xcb75c3de, "pci_dev_put" },
	{ 0xceb38aab, "register_kretprobe" },
	{ 0xedc03953, "iounmap" },
	{ 0x35273366, "unregister_kretprobe" },
	{ 0x3c12dfe, "cancel_work_sync" },
	{ 0x9fa7184a, "cancel_delayed_work_sync" },
	{ 0xb6c4379f, "system_percpu_wq" },
	{ 0xc5b6f236, "queue_work_on" },
	{ 0x15ba50a6, "jiffies" },
	{ 0xf5edea2e, "___ratelimit" },
	{ 0x16e9455a, "pcie_capability_read_word" },
	{ 0x7f02188f, "__msecs_to_jiffies" },
	{ 0xb2fcb56d, "queue_delayed_work_on" },
	{ 0x5c6c6486, "pcie_capability_write_word" },
	{ 0xf9a482f9, "msleep" },
	{ 0x5233a6cc, "param_ops_bool" },
	{ 0x54b1a1a2, "param_ops_int" },
	{ 0xbdfb6dbb, "__fentry__" },
	{ 0xd6a53aa, "filp_open" },
	{ 0x817dfe87, "kernel_read" },
	{ 0xe55eedee, "filp_close" },
	{ 0x8c8569cb, "kstrtoint" },
	{ 0x5b8239ca, "__x86_return_thunk" },
	{ 0x7fe2a4c3, "module_layout" },
};

MODULE_INFO(depends, "");

