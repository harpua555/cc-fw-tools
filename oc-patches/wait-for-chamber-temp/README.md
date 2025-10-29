# TEMPERATURE_WAIT Box Sensor Patch

Adds chamber ('box') temperature support to TEMPERATURE_WAIT without changing stock heater behavior. The patch diverts only if the SENSOR provided is not found in *Heaters

## What This Enables
TEMPERATURE_WAIT SENSOR=box MINIMUM=X now waits until chamber temperature ≥ X and returns.
Printer/screen/webUI remains responsive during wait.

## How to use
```
TEMPERATURE_WAIT SENSOR=box MINIMUM=XX MAXIMUM=YY
```
New function is hard-coded to chamber temp for now.  Both MINIMUM and MAXIMUM must be provided, however only MINIMUM is assessed  
-example - 
```
TEMPERATURE_WAIT SENSOR=box MINIMUM=45 MAXIMUM=60
```
--- This will wait (indefinitely) for the chamber temp to reach 45 before proceeding to the next line of gcode

## TODO:
-Enable MAXIMUM evaluation as well as minimum  
-Ensure arg1 is 'box', return if not  
-Emit error to log for the above  
-Patch stock (heater) branch of TEMPERATURE_WAIT to prevent soft-locking while waiting

## Technical:

Addresses Patched  
0x00165A30 – becomes a jump to new code at 0x00391EC0  
New code range: 0x00391EC0–0x00391F34

Patched Original Bytes  
```
0x00165A30  EB FD F2 74 => 22 B1 08 EA
```
bl sub_e2408  →  b 0x00391EC0

Used:  
simple_bus_request to get chamber temp  
usleep to wait without freezing  
existing registers and calls from TEMPERATURE_WAIT