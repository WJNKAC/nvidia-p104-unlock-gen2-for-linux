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
	{ 0x456b7f34, "pci_get_device" },
	{ 0xcb75c3de, "pci_dev_put" },
	{ 0x5b8239ca, "__x86_return_thunk" },
	{ 0xbb10e61d, "unregister_kprobe" },
	{ 0xf5edea2e, "___ratelimit" },
	{ 0x4e4cd9d0, "param_ops_charp" },
	{ 0xbdfb6dbb, "__fentry__" },
	{ 0x3f66a26e, "register_kprobe" },
	{ 0x92997ed8, "_printk" },
	{ 0x7fe2a4c3, "module_layout" },
};

MODULE_INFO(depends, "");

