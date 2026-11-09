"""amd-gpu-oc-toolkit (amdoc) — AMD GPU overclocking & monitoring for Linux.

Works entirely through the amdgpu driver's sysfs interface:
  * overdrive clock/voltage table  (/sys/class/drm/cardX/device/pp_od_clk_voltage)
  * DPM state control              (/sys/class/drm/cardX/device/pp_dpm_*)
  * fan control                    (hwmon pwm1/pwm1_enable)
  * power limit                    (hwmon power1_cap)
  * sensor monitoring              (hwmon temp/freq/power + device attributes)
"""

__version__ = "0.1.0"
