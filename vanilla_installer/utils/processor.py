# processor.py
#
# Copyright 2024 mirkobrombin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundationat version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Union

from vanilla_installer.core.disks import Diskutils
from vanilla_installer.core.system import Systeminfo

logger = logging.getLogger("Installer::Processor")

# fmt: off
_BASE_DIRS = ["boot", "dev", "home", "media", "mnt", "var", "opt",
              "part-future", "proc", "root", "run", "srv", "sys", "tmp"]
_REL_LINKS = ["usr", "etc", "usr/bin", "usr/lib",
              "usr/lib32", "usr/lib64", "usr/libx32", "usr/sbin"]
_REL_SYSTEM_LINKS = ["dev", "proc", "run", "srv", "sys", "media"]
# fmt: on

_ROOT_GRUB_CFG = """insmod gzio
insmod part_gpt
insmod ext2
linux   (lvm/vos--root-init)/vos-a/vmlinuz-%s root=%s quiet splash bgrt_disable $vt_handoff lsm=integrity
initrd  (lvm/vos--root-init)/vos-a/initrd.img-%s
"""

_BOOT_GRUB_CFG = """set default=0
set timeout=5

### BEGIN /etc/grub.d/00_header ###
if [ -s $prefix/grubenv ]; then
  set have_grubenv=true
  load_env
fi

if [ x"${feature_menuentry_id}" = xy ]; then
  menuentry_id_option="--id"
else
  menuentry_id_option=""
fi

export menuentry_id_option

if [ "${prev_saved_entry}" ]; then
  set saved_entry="${prev_saved_entry}"
  save_env saved_entry
  set prev_saved_entry=
  save_env prev_saved_entry
  set boot_once=true
fi

function savedefault {
  if [ -z "${boot_once}" ]; then
    saved_entry="${chosen}"
    save_env saved_entry
  fi
}
function load_video {
  if [ x$feature_all_video_module = xy ]; then
    insmod all_video
  else
    insmod efi_gop
    insmod efi_uga
    insmod ieee1275_fb
    insmod vbe
    insmod vga
    insmod video_bochs
    insmod video_cirrus
  fi
}

font=unicode

if loadfont $font ; then
  set gfxmode=auto
  load_video
  insmod gfxterm
  set locale_dir=$prefix/locale
  set lang=en_US
  insmod gettext
fi
terminal_output gfxterm
if [ "${recordfail}" = 1 ] ; then
  set timeout=30
else
  if [ x$feature_timeout_style = xy ] ; then
    set timeout_style=menu
    set timeout=5
  # Fallback normal timeout code in case the timeout_style feature is
  # unavailable.
  else
    set timeout=5
  fi
fi
### END /etc/grub.d/00_header ###


### BEGIN /etc/grub.d/10_linux ###
function gfxmode {
	set gfxpayload="${1}"
}
set linux_gfx_mode=
export linux_gfx_mode
### END /etc/grub.d/10_linux ###

# AUTO GENERATED BY ABROOT
menuentry "Current State (A)" --class abroot-a {
    set root=(lvm/vos--root-init)
    configfile "/vos-a/abroot.cfg"
}

menuentry "Previous State (B)" --class abroot-b {
    set root=(lvm/vos--root-init)
    configfile "/vos-b/abroot.cfg"
}
# END - AUTO GENERATED BY ABROOT
"""

_ABIMAGE_FILE = """{
    "digest":"%s",
    "timestamp":"%s",
    "image":"%s"
}
"""

_SYSTEMD_MOUNT_UNIT = """[Unit]
Description=Mounts %s from var
After=local-fs-pre.target %s
Before=local-fs.target nss-user-lookup.target
RequiresMountsFor=/var

[Mount]
What=%s
Where=%s
Type=%s
Options=%s
"""

systemd_mount_unit_contents = [
    ["/var/home", "/home", "none", "bind", "home.mount"],
    ["/var/opt", "/opt", "none", "bind", "opt.mount"],
    [
        "/var/lib/abroot/etc/vos-a/locales",
        "/.system/usr/lib/locale",
        "none",
        "bind",
        "\\x2esystem-usr-lib-locale.mount",
    ],
    [
        "overlay",
        "/.system/etc",
        "overlay",
        "lowerdir=/.system/etc,upperdir=/var/lib/abroot/etc/vos-a,workdir=/var/lib/abroot/etc/vos-a-work",
        "\\x2esystem-etc.mount",
    ],
]


AlbiusSetupStep = dict[str, Union[str, list[Any]]]
AlbiusMountpoint = dict[str, str]
AlbiusInstallation = dict[str, str | list[str]]
AlbiusPostInstallStep = dict[str, Union[bool, str, list[Any]]]


class AlbiusRecipe:
    def __init__(self) -> None:
        self.setup: list[AlbiusSetupStep] = []
        self.mountpoints: list[AlbiusMountpoint] = []
        self.installation: AlbiusInstallation = {}
        self.postInstallation: list[AlbiusPostInstallStep] = []
        self.latePostInstallation: list[AlbiusPostInstallStep] = []

    def add_setup_step(self, disk: str, operation: str, params: list[Any]) -> None:
        self.setup.append(
            {
                "disk": disk,
                "operation": operation,
                "params": params,
            }
        )

    def add_mountpoint(self, partition: str, target: str) -> None:
        self.mountpoints.append(
            {
                "partition": partition,
                "target": target,
            }
        )

    def set_installation(self, method: str, source: str) -> None:
        self.installation = {
            "method": method,
            "source": source,
            "initramfsPre": ["lpkg --unlock"],
            "initramfsPost": ["lpkg --lock"],
        }

    def add_postinstall_step(
        self, operation: str, params: list[Any], chroot: bool = False, late=False
    ):
        if not late:
            self.postInstallation.append(
                {
                    "chroot": chroot,
                    "operation": operation,
                    "params": params,
                }
            )
        else:
            self.latePostInstallation.append(
                {
                    "chroot": chroot,
                    "operation": operation,
                    "params": params,
                }
            )

    def merge_postinstall_steps(self):
        for step in self.latePostInstallation:
            self.postInstallation.append(step)
        del self.latePostInstallation


class Processor:
    @staticmethod
    def __gen_auto_partition_steps(
        disk: str,
        encrypt: bool,
        root_size: int,
        existing_pvs: list[str] | None,
        existing_vgs: list[str] | None,
        password: str | None = None,
    ):
        setup_steps = []
        mountpoints = []
        post_install_steps = []

        # Before we do anything, we need to remove conflicting LVM objects
        for vg in existing_vgs:
            setup_steps.append([disk, "vgremove", [vg]])
        for pv in existing_pvs:
            setup_steps.append([disk, "pvremove", [pv]])

        setup_steps.append([disk, "label", ["gpt"]])
        # Boot
        setup_steps.append([disk, "mkpart", ["vos-boot", "ext4", 1, 1025]])
        setup_steps.append([disk, "mkpart", ["vos-efi", "fat32", 1025, 1537]])
        setup_steps.append([disk, "setflag", ["2", "esp", True]])

        # LVM PVs
        setup_steps.append([disk, "mkpart", ["vos-root", "none", 1537, 23556]])
        setup_steps.append([disk, "mkpart", ["vos-var", "none", 23556, -1]])
        part_prefix = f"{disk}p" if re.match(r"[0-9]", disk[-1]) else f"{disk}"
        setup_steps.append([disk, "pvcreate", [part_prefix + "3"]])
        setup_steps.append([disk, "pvcreate", [part_prefix + "4"]])

        # LVM VGs
        setup_steps.append([disk, "vgcreate", ["vos-root", [part_prefix + "3"]]])
        setup_steps.append([disk, "vgcreate", ["vos-var", [part_prefix + "4"]]])

        # Init files LV
        setup_steps.append([disk, "lvcreate", ["init", "vos-root", "linear", 512]])
        setup_steps.append([disk, "lvm-format", ["vos-root/init", "ext4", "vos-init"]])

        # LVM root thin pool
        setup_steps.append([disk, "lvcreate", ["root", "vos-root", "linear", 19456]])
        setup_steps.append(
            [disk, "lvcreate", ["root-meta", "vos-root", "linear", 1024]]
        )
        setup_steps.append(
            [
                disk,
                "make-thin-pool",
                ["vos-root/root", "vos-root/root-meta"],
            ]
        )
        setup_steps.append(
            [disk, "lvcreate-thin", ["root-a", "vos-root", 19456, "root"]]
        )
        setup_steps.append(
            [disk, "lvcreate-thin", ["root-b", "vos-root", 19456, "root"]]
        )
        setup_steps.append([disk, "lvm-format", ["vos-root/root-a", "btrfs", "vos-a"]])
        setup_steps.append([disk, "lvm-format", ["vos-root/root-b", "btrfs", "vos-b"]])

        # LVM var
        setup_steps.append([disk, "lvcreate", ["var", "vos-var", "linear", "100%FREE"]])
        lvm_var_args = ["vos-var/var", "btrfs", "vos-var"]
        if encrypt:
            assert password
            lvm_var_args.insert(2, password)
        setup_steps.append(
            [disk, "lvm-luks-format" if encrypt else "lvm-format", lvm_var_args]
        )

        # Mountpoints
        if not re.match(r"[0-9]", disk[-1]):
            part_prefix = f"{disk}"
        else:
            part_prefix = f"{disk}p"

        mountpoints.append([part_prefix + "1", "/boot"])

        if Systeminfo.is_uefi():
            mountpoints.append([part_prefix + "2", "/boot/efi"])

        mountpoints.append(["/dev/vos-root/root-a", "/"])
        mountpoints.append(["/dev/vos-root/root-b", "/"])
        mountpoints.append(["/dev/vos-var/var", "/var"])

        return setup_steps, mountpoints, post_install_steps, disk

    @staticmethod
    def __gen_manual_partition_steps(
        disk_final: dict, encrypt: bool, password: str | None = None
    ):
        setup_steps = []
        mountpoints = []
        post_install_steps = []

        # Before we do anything, we need to remove conflicting LVM objects
        vgs_to_remove = []
        pvs_to_remove = []
        for part, values in disk_final.items():
            pv = values["existing_pv"]
            vg = values["existing_vg"]
            if pv is None:
                continue
            disk, _ = Diskutils.separate_device_and_partn(pv)
            pvs_to_remove.append([pv, disk])
            if vg is not None and vg not in vgs_to_remove:
                vgs_to_remove.append([vg, disk])

        for vg, disk in vgs_to_remove:
            setup_steps.append([disk, "vgremove", [vg]])
        for pv, disk in pvs_to_remove:
            setup_steps.append([disk, "pvremove", [pv]])

        boot_disk = None

        # Since manual partitioning uses GParted to handle partitions (for now),
        # we don't need to create any partitions or label disks (for now).
        # But we still need to format partitions.
        for part, values in disk_final.items():
            part_disk, part_number = Diskutils.separate_device_and_partn(part)

            def setup_partition(
                part_name: str, encrypt: bool = False, password: str | None = None
            ):
                format_args = [part_number, values["fs"]]
                if encrypt:
                    operation = "luks-format"
                    assert password is not None
                    format_args.append(password)
                else:
                    operation = "format"
                format_args.append(part_name)
                setup_steps.append([part_disk, operation, format_args])
                mountpoints.append([part, values["mp"]])

            if values["mp"] == "/":
                setup_steps.append([part_disk, "pvcreate", [part]])
                setup_steps.append([part_disk, "vgcreate", ["vos-root", [part]]])
                setup_steps.append(
                    [part_disk, "lvcreate", ["init", "vos-root", "linear", 512]]
                )
                setup_steps.append(
                    [part_disk, "lvm-format", ["vos-root/init", "ext4", "vos-init"]]
                )

                # LVM root thin pool
                # Total pool size is the disk size, subtracted by:
                # - 512 MiB from the init LV
                # - 1024 MiB from the metadata LV
                # - 1028 MiB from LVM's internals (4 MiB header and 1024 MiB for thin)
                thin_size = (values["size"] / 1_048_576) - 1024 - 512 - 1028
                setup_steps.append(
                    [part_disk, "lvcreate", ["root-meta", "vos-root", "linear", 1024]]
                )
                setup_steps.append(
                    [part_disk, "lvcreate", ["root", "vos-root", "linear", thin_size]]
                )
                setup_steps.append(
                    [
                        part_disk,
                        "make-thin-pool",
                        ["vos-root/root", "vos-root/root-meta"],
                    ]
                )
                setup_steps.append(
                    [
                        part_disk,
                        "lvcreate-thin",
                        ["root-a", "vos-root", thin_size, "root"],
                    ]
                )
                setup_steps.append(
                    [
                        part_disk,
                        "lvcreate-thin",
                        ["root-b", "vos-root", thin_size, "root"],
                    ]
                )
                setup_steps.append(
                    [part_disk, "lvm-format", ["vos-root/root-a", "btrfs", "vos-a"]]
                )
                setup_steps.append(
                    [part_disk, "lvm-format", ["vos-root/root-b", "btrfs", "vos-b"]]
                )
                mountpoints.append(["/dev/vos-root/root-a", "/"])
                mountpoints.append(["/dev/vos-root/root-b", "/"])
            elif values["mp"] == "/boot":
                setup_partition("vos-boot")
                boot_disk = part_disk
            elif values["mp"] == "/boot/efi":
                setup_partition("vos-efi")
                setup_steps.append([part_disk, "setflag", [part_number, "esp", True]])
            elif values["mp"] == "/var":
                setup_partition("vos-var", encrypt, password)
            elif values["mp"] == "swap":
                post_install_steps.append(["swapon", [part], True])

        return setup_steps, mountpoints, post_install_steps, boot_disk

    @staticmethod
    def __find_partitions(recipe: AlbiusRecipe) -> tuple[str, str, str, str, str]:
        boot_partition = ""
        efi_partition = ""
        root_a_partition = ""
        root_b_partition = ""
        var_partition = ""

        for mnt in recipe.mountpoints:
            if mnt["target"] == "/boot":
                boot_partition = mnt["partition"]
            elif mnt["target"] == "/boot/efi":
                efi_partition = mnt["partition"]
            elif mnt["target"] == "/":
                if not root_a_partition:
                    root_a_partition = mnt["partition"]
                else:
                    root_b_partition = mnt["partition"]
            elif mnt["target"] == "/var":
                var_partition = mnt["partition"]

        return (
            boot_partition,
            efi_partition,
            root_a_partition,
            root_b_partition,
            var_partition,
        )

    @staticmethod
    def gen_install_recipe(log_path, finals, sys_recipe):
        logger.info("processing the following final data: %s", finals)

        recipe = AlbiusRecipe()

        images = sys_recipe.get("images")
        root_size = sys_recipe.get("default_root_size")
        oci_image = images["default"]

        # Setup encryption if user selected it
        encrypt = False
        password = None
        for final in finals:
            if "encryption" in final.keys():
                encrypt = final["encryption"]["use_encryption"]
                password = final["encryption"]["encryption_key"] if encrypt else None

        boot_disk = None

        # Setup disks and mountpoints
        for final in finals:
            if "disk" in final.keys():
                if "auto" in final["disk"].keys():
                    part_info = Processor.__gen_auto_partition_steps(
                        final["disk"]["auto"]["disk"],
                        encrypt,
                        root_size,
                        final["disk"]["auto"]["pvs_to_remove"],
                        final["disk"]["auto"]["vgs_to_remove"],
                        password,
                    )
                else:
                    part_info = Processor.__gen_manual_partition_steps(
                        final["disk"], encrypt, password
                    )

                setup_steps, mountpoints, post_install_steps, boot_disk = part_info
                for step in setup_steps:
                    recipe.add_setup_step(*step)
                for mount in mountpoints:
                    recipe.add_mountpoint(*mount)
                for step in post_install_steps:
                    recipe.add_postinstall_step(*step)
            elif "nvidia" in final.keys():
                if final["nvidia"]["use-proprietary"]:
                    oci_image = images["nvidia"]
            elif "vm" in final.keys():
                if final["vm"]["use-vm-tools"]:
                    oci_image = images["vm"]

        # Installation
        recipe.set_installation("oci", oci_image)

        # Post-installation
        (
            boot_part,
            efi_part,
            root_a_part,
            root_b_part,
            var_part,
        ) = Processor.__find_partitions(recipe)

        # Create SystemD units to setup mountpoints
        extra_target = "cryptsetup" if encrypt else ""
        for systemd_mount in systemd_mount_unit_contents:
            source = systemd_mount[0]
            destination = systemd_mount[1]
            fs_type = systemd_mount[2]
            options = systemd_mount[3]
            filename = systemd_mount[4]
            filename_escaped = filename.replace("\\", "\\\\")
            with open("/tmp/" + filename, "w") as file:
                file.write(
                    _SYSTEMD_MOUNT_UNIT
                    % (destination, extra_target, source, destination, fs_type, options)
                )
            recipe.add_postinstall_step(
                "shell",
                [
                    f"cp /tmp/{filename_escaped} /mnt/a/etc/systemd/system/{filename_escaped}",
                    "mkdir -p /mnt/a/etc/systemd/system/local-fs.target.wants",
                    f"ln -s ../{filename_escaped} /mnt/a/etc/systemd/system/local-fs.target.wants/{filename_escaped}",
                ],
            )

        if "VANILLA_SKIP_POSTINSTALL" not in os.environ:
            # Adapt root A filesystem structure
            if encrypt:
                var_label = f"/dev/mapper/luks-$(lsblk -d -y -n -o UUID {var_part})"
            else:
                var_label = var_part
            recipe.add_postinstall_step(
                "shell",
                [
                    "umount /mnt/a/var",
                    "mkdir /mnt/a/tmp-boot",
                    "cp -r /mnt/a/boot /mnt/a/tmp-boot",
                    f"umount -l {boot_part}",
                    "mkdir -p /mnt/a/.system",
                    "mv /mnt/a/* /mnt/a/.system/",
                    "mv /mnt/a/.system/tmp-boot/boot/* /mnt/a/.system/boot",
                    "rm -rf /mnt/a/.system/tmp-boot",
                    *[f"mkdir -p /mnt/a/{path}" for path in _BASE_DIRS],
                    *[f"ln -rs /mnt/a/.system/{path} /mnt/a/" for path in _REL_LINKS],
                    *[f"rm -rf /mnt/a/.system/{path}" for path in _REL_SYSTEM_LINKS],
                    *[
                        f"ln -rs /mnt/a/{path} /mnt/a/.system/"
                        for path in _REL_SYSTEM_LINKS
                    ],
                    f"mount {var_label} /mnt/a/var",
                    f"mount {boot_part} /mnt/a/boot{f' && mount {efi_part} /mnt/a/boot/efi' if efi_part else ''}",
                ],
            )

            # Create default user
            # This needs to be done after mounting `/etc` overlay, so set it as
            # late post-install
            recipe.add_postinstall_step(
                "adduser",
                [
                    "vanilla",
                    "vanilla",
                    ["sudo", "lpadmin"],
                    "vanilla",
                ],
                chroot=True,
                late=True,
            )

            # Set vanilla user to autologin
            recipe.add_postinstall_step(
                "shell",
                [
                    "mkdir -p /etc/gdm3",
                    "echo '[daemon]\nAutomaticLogin=vanilla\nAutomaticLoginEnable=True' > /etc/gdm3/daemon.conf",
                    "mkdir -p /home/vanilla/.config/dconf",
                    "chmod 700 /home/vanilla/.config/dconf",
                ],
                chroot=True,
            )

            # Make sure the vanilla user uses the first-setup session
            recipe.add_postinstall_step(
                "shell",
                [
                    "mkdir -p /var/lib/AccountsService/users",
                    "echo '[User]\nSession=firstsetup' > /var/lib/AccountsService/users/vanilla",
                ],
                chroot=True,
            )

            # Add autostart script to vanilla-first-setup
            recipe.add_postinstall_step(
                "shell",
                [
                    "mkdir -p /home/vanilla/.config/autostart",
                    "cp /usr/share/applications/org.vanillaos.FirstSetup.desktop /home/vanilla/.config/autostart",
                ],
                chroot=True,
                late=True,
            )

            # TODO: Install grub-pc if target is BIOS
            # Run `grub-install` with the boot partition as target
            grub_type = "efi" if Systeminfo.is_uefi() else "bios"
            recipe.add_postinstall_step(
                "grub-install", ["/mnt/a/boot", boot_disk, grub_type, efi_part]
            )
            recipe.add_postinstall_step(
                "grub-install", ["/boot", boot_disk, grub_type, efi_part], chroot=True
            )

            # Run `grub-mkconfig` to generate files for the boot partition
            recipe.add_postinstall_step(
                "grub-mkconfig", ["/boot/grub/grub.cfg"], chroot=True
            )

            # Replace main GRUB entry in the boot partition
            with open("/tmp/boot-grub.cfg", "w") as file:
                file.write(_BOOT_GRUB_CFG)
            recipe.add_postinstall_step(
                "shell", ["cp /tmp/boot-grub.cfg /mnt/a/boot/grub/grub.cfg"]
            )

            # Unmount boot partition so we can modify the root GRUB config
            recipe.add_postinstall_step(
                "shell", ["umount -l /mnt/a/boot", "mkdir -p /mnt/a/boot/grub"]
            )

            # Run `grub-mkconfig` inside the root partition
            recipe.add_postinstall_step(
                "grub-mkconfig", ["/boot/grub/grub.cfg"], chroot=True
            )

            # Copy init files to init LV
            recipe.add_postinstall_step(
                "shell",
                [
                    "mkdir /.system/boot/init",
                    "mount /dev/vos-root/init /.system/boot/init",
                    "mkdir /.system/boot/init/vos-a",
                    "mkdir /.system/boot/init/vos-b",
                    "mv /.system/boot/vmlinuz* /.system/boot/init/vos-a",
                    "mv /.system/boot/initrd* /.system/boot/init/vos-a",
                ],
                chroot=True,
            )

            # Add `/boot/grub/abroot.cfg` to the root partition
            with open("/tmp/abroot.cfg", "w") as file:
                root_entry = _ROOT_GRUB_CFG % (
                    "$KERNEL_VERSION",
                    "UUID=$ROOTA_UUID",
                    "$KERNEL_VERSION",
                )
                file.write(root_entry)
            recipe.add_postinstall_step(
                "shell",
                [
                    " ".join(
                        f"BOOT_UUID=$(lsblk -d -n -o UUID {boot_part}) \
                        ROOTA_UUID=$(lsblk -d -n -o UUID {root_a_part}) \
                        KERNEL_VERSION=$(ls -1 /mnt/a/usr/lib/modules | sed '1p;d') \
                        envsubst < /tmp/abroot.cfg > /mnt/a/.system/boot/init/vos-a/abroot.cfg \
                        '$BOOT_UUID $ROOTA_UUID $KERNEL_VERSION'".split()
                    )
                ],
            )

            # Delete everything but root A entry from fstab and add /.system/usr and /var mounts
            var_location_prefix = "/dev/mapper/luks-" if encrypt else "UUID="
            fstab_regex = r"/^[^#]\S+\s+\/\S+\s+.+$/d"
            recipe.add_postinstall_step(
                "shell",
                [
                    f'ROOTB_UUID=$(lsblk -d -y -n -o UUID {root_b_part}) && sed -i "/UUID=$ROOTB_UUID/d" /mnt/a/etc/fstab',
                    f"sed -i -r '{fstab_regex}' /mnt/a/etc/fstab",
                    "echo '/.system/usr  /.system/usr  none  bind,ro' >> /mnt/a/etc/fstab",
                    f'VAR_UUID=$(lsblk -d -n -o UUID {var_part}) && echo "{var_location_prefix}$VAR_UUID /var  auto  defaults  0  0" >> /mnt/a/etc/fstab',
                ],
            )

            # Mount `/etc` as overlay; `/home`, `/opt` and `/usr` as bind
            recipe.add_postinstall_step(
                "shell",
                [
                    "mv /.system/home /var",
                    "mv /.system/opt /var",
                    "mv /.system/tmp /var",
                    "mkdir -p /var/lib/abroot/etc/vos-a /var/lib/abroot/etc/vos-b /var/lib/abroot/etc/vos-a-work /var/lib/abroot/etc/vos-b-work",
                    "mount -t overlay overlay -o lowerdir=/.system/etc,upperdir=/var/lib/abroot/etc/vos-a,workdir=/var/lib/abroot/etc/vos-a-work /etc",
                    "mv /var/storage /var/lib/abroot/",
                    "mount -o bind /var/home /home",
                    "mount -o bind /var/opt /opt",
                    "mount -o bind,ro /.system/usr /usr",
                    "mkdir -p /var/lib/abroot/etc/vos-a/locales",
                    "mount -o bind /var/lib/abroot/etc/vos-a/locales /usr/lib/locale",
                ],
                chroot=True,
            )

        # Set hostname
        recipe.add_postinstall_step("hostname", ["vanilla"], chroot=True)
        for final in finals:
            for key, value in final.items():
                # Set timezone
                if key == "timezone":
                    recipe.add_postinstall_step(
                        "timezone", [f"{value['region']}/{value['zone']}"], chroot=True
                    )
                # Set locale
                if key == "language":
                    recipe.add_postinstall_step("locale", [value], chroot=True)
                # Set keyboard
                if key == "keyboard":
                    for i in value:
                        recipe.add_postinstall_step(
                            "keyboard",
                            [
                                i["layout"],
                                i["model"],
                                i["variant"],
                            ],
                            chroot=True,
                        )

        # Create /abimage.abr
        with open("/tmp/abimage.abr", "w") as file:
            abimage = _ABIMAGE_FILE % (
                "$IMAGE_DIGEST",
                datetime.now().astimezone().isoformat(),
                oci_image,
            )
            file.write(abimage)

        recipe.add_postinstall_step(
            "shell",
            [
                " ".join(
                    "IMAGE_DIGEST=$(cat /mnt/a/.oci_digest) \
                    envsubst < /tmp/abimage.abr > /mnt/a/abimage.abr \
                    '$IMAGE_DIGEST'".split()
                )
            ],
        )

        # Set the default user as the owner of it's home directory
        recipe.add_postinstall_step(
            "shell",
            ["chown -R vanilla:vanilla /home/vanilla"],
            chroot=True,
            late=True,
        )

        # Set ABRoot Thin-Provisioning option
        recipe.add_postinstall_step(
            "shell",
            [
                "mkdir -p /etc/abroot",
                'echo "$(head -n-1 /usr/share/abroot/abroot.json),\n    \\"thinProvisioning\\": true,\n    \\"thinInitVolume\\": \\"vos-init\\"\n}" > /etc/abroot/abroot.json',
            ],
            chroot=True,
        )

        recipe.merge_postinstall_steps()

        if "VANILLA_FAKE" in os.environ:
            logger.info(json.dumps(recipe, default=vars))
            return None

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(json.dumps(recipe, default=vars))
            f.flush()
            f.close()

            # setting the file executable
            os.chmod(f.name, 0o755)

            return f.name
