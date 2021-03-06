from bcc import BPF
from time import sleep
from subprocess import Popen
import argparse
import sys

bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/bio.h>

BPF_HASH(fail_pids, u64);

int trigger_function(struct pt_regs *ctx)
{
    u64 pid = bpf_get_current_pid_tgid();
    u64 zero = 0;
    u64 *val;
    val = fail_pids.lookup_or_init(&pid, &zero);
    lock_xadd(val, 1);
    return 0;
}

int trigger_function_ret(struct pt_regs *ctx)
{
    u64 pid = bpf_get_current_pid_tgid();
    u64 *val;
    val = fail_pids.lookup(&pid);
    if (!val)
        return 0;
    lock_xadd(val, -1);
    return 0;
}

int override_function(struct pt_regs *ctx)
{
    u64 pid = bpf_get_current_pid_tgid();
    u64 *val;

    val = fail_pids.lookup(&pid);
    if (!val)
        return 0;
    if (*val != FAIL_CNT)
        return 0;

    bpf_trace_printk("overrding something\\n");
    unsigned long rc = RCVAL;
    bpf_override_return(ctx, rc);
    return 0;
}
"""

parser = argparse.ArgumentParser()
parser.add_argument("-o", "--override", help="The function to override")
parser.add_argument("-r", "--retval", type=int, help="The return value to use")
parser.add_argument("-t", "--trigger", action='append',
    help="The function that must be called to trigger the error injection")
parser.add_argument("-d", "--delay", type=int,
    help="The delay to wait before injecting the error")
parser.add_argument("-T", "--timeout", type=int,
    help="Timeout after error injection has been loaded to wait on the task")
parser.add_argument("COMMAND", nargs='+', help="The command to run")

args = parser.parse_args()
retval = -12

if not args.override:
    print("Must specify an override function")
    sys.exit(1)
if not args.trigger:
    print("Must specify a function as the trigger function")
    sys.exit(1)
if args.retval:
    retval = args.retval

bpf_text = bpf_text.replace("RCVAL", str(retval))
bpf_text = bpf_text.replace("FAIL_CNT", str(len(args.trigger)))

print("Running command")
p = Popen(args.COMMAND)
if args.delay:
    print("Sleeping for {} seconds".format(args.delay))
    sleep(args.delay)

print("Loading error injection")
b = BPF(text=bpf_text)

# Load the kretprobe first, because we want the delete guy to be in place before
# the add guy is in place, otherwise we could error out pids that are no longer
# in our path and cause unfortunate things to happen.
for t in args.trigger:
    b.attach_kretprobe(event=t, fn_name="trigger_function_ret")
for t in args.trigger:
    b.attach_kprobe(event=t, fn_name="trigger_function")
b.attach_kprobe(event=args.override, fn_name="override_function")

print("Dropping caches")
f = open("/proc/sys/vm/drop_caches", "w")
f.write("3")
f.close()

print("Waiting for the command to exit")
while p.poll() is None:
    if args.timeout:
        sleep(args.timeout)
        if p.poll() is None:
            print("Killing the task, it didn't die")
            f = open("nofail.txt", "a")
            f.write(args.trigger + "\n")
            f.close()
            p.kill()
            p.wait()
        break
    p.wait()

# We have to remove in this order otherwise we could end up with a half
# populated hasmap and overrding legitimate things.
b.detach_kprobe(args.override)
for t in args.trigger:
    b.detach_kprobe(t)
for t in args.trigger:
    b.detach_kretprobe(t)
print("Exiting")
