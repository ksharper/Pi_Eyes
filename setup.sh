#!/bin/bash

if [ $(id -u) -ne 0 ]; then
  echo "Installer must be run as root."
  echo "Try 'sudo bash $0'"
  exit 1
fi

# START INSTALL ------------------------------------------------------------
# All selections are validated at this point...

# Given a filename, a regex pattern to match and a replacement string,
# perform replacement if found, else append replacement to end of file.
# (# $1 = filename, $2 = pattern to match, $3 = replacement)
reconfig() {
  grep $2 $1 >/dev/null
  if [ $? -eq 0 ]; then
    # Pattern found; replace in file
    sed -i "s/$2/$3/g" $1 >/dev/null
  else
    # Not found; append (silently)
    echo $3 | sudo tee -a $1 >/dev/null
  fi
}

# Same as above, but appends to same line rather than new line
reconfig2() {
  grep $2 $1 >/dev/null
  if [ $? -eq 0 ]; then
    # Pattern found; replace in file
    sed -i "s/$2/$3/g" $1 >/dev/null
  else
    # Not found; append to line (silently)
    sed -i "s/$/ $3/g" $1 >/dev/null
  fi
}

echo
echo "Starting installation..."
echo "Updating package index files..."
apt-get update

echo "Installing Python libraries..."
apt-get install -y python3-pip python3-dev python3-pil libatlas-base-dev
pip3 install numpy pi3d svg.path

# CONFIG -------------------------------------------------------------------

echo "Configuring system..."

# Make desktop system to boot to console (from raspi-config script):
systemctl set-default multi-user.target
ln -fs /lib/systemd/system/getty@.service /etc/systemd/system/getty.target.wants/getty@tty1.service
rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf

# Pi3D requires "fake" KMS overlay to work. Check /boot/config.txt for
# vc4-fkms-v3d overlay present and active. If so, nothing to do here,
# module's already configured.
grep '^dtoverlay=vc4-fkms-v3d' /boot/config.txt >/dev/null
if [ $? -ne 0 ]; then
# fkms overlay not present, or is commented out. Check if vc4-kms-v3d
# (no 'f') is present and active. That's normally the default.
grep '^dtoverlay=vc4-kms-v3d' /boot/config.txt >/dev/null
if [ $? -eq 0 ]; then
    # It IS present. Comment out that line for posterity, and insert the
    # 'fkms' item on the next line.
    sed -i "s/^dtoverlay=vc4-kms-v3d/#&\ndtoverlay=vc4-fkms-v3d/g" /boot/config.txt >/dev/null
else
    # It's NOT present. Silently append 'fkms' overlay to end of file.
    echo dtoverlay=vc4-fkms-v3d | sudo tee -a /boot/config.txt >/dev/null
fi
fi

# Disable screen blanking in X config
echo "Section \"ServerFlags\"
Option \"BlankTime\" \"0\"
Option \"StandbyTime\" \"0\"
Option \"SuspendTime\" \"0\"
Option \"OffTime\" \"0\"
Option \"dpms\" \"false\"
EndSection" > /etc/X11/xorg.conf

# Disable overscan compensation (use full screen):
raspi-config nonint do_overscan 1

# Dedicate 128 MB to the GPU:
sudo raspi-config nonint do_memory_split 128

# HDMI settings for Pi eyes
reconfig /boot/config.txt "^.*hdmi_force_hotplug.*$" "hdmi_force_hotplug=1"
reconfig /boot/config.txt "^.*hdmi_group.*$" "hdmi_group=2"
reconfig /boot/config.txt "^.*hdmi_mode.*$" "hdmi_mode=87"
reconfig /boot/config.txt "^.*hdmi_cvt.*$" "hdmi_cvt=640 480 60 1 0 0 0"