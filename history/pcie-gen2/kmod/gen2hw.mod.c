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
	{ 0x92997ed8, "_printk" },
	{ 0xf50035a7, "const_pcpu_hot" },
	{ 0x17de3d5, "nr_cpu_ids" },
	{ 0x5a5a2271, "__cpu_online_mask" },
	{ 0x53a1e8d9, "_find_next_bit" },
	{ 0x1cf8809e, "perf_event_create_kernel_counter" },
	{ 0x21e346e0, "perf_event_enable" },
	{ 0xf0fdf6cb, "__stack_chk_fail" },
	{ 0x9ae5c7b, "perf_event_disable" },
	{ 0x1f333fb, "perf_event_release_kernel" },
	{ 0x54b1a1a2, "param_ops_int" },
	{ 0x9e233727, "param_ops_ulong" },
	{ 0xbdfb6dbb, "__fentry__" },
	{ 0x5b8239ca, "__x86_return_thunk" },
	{ 0xf5edea2e, "___ratelimit" },
	{ 0x7fe2a4c3, "module_layout" },
};

MODULE_INFO(depends, "");

