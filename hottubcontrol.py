#!/usr/bin/python3
#
#10/22/2017 Steven Kaye

import I2C_LCD_driver #Note that this file contains the I2C address and certain settings
import RPi.GPIO as GPIO
#from ds18b20 import DS18B20
from w1thermsensor import W1ThermSensor
import time
import sys
import threading


#Set configurable constants
##Output pins - BCM numbering##
pumpLowPin = 9
pumpHighPin = 10
heaterPin = 11
blowerPin = 8
lightPin = 7
buttonLedPin = 4

##Input pins - BCM numbering##
pumpButtonPin = 25
blowerButtonPin = 14 
lightButtonPin = 15
modeButtonPin = 17
tempUpButtonPin = 18
tempDownButtonPin = 23

##User options##
debug = 0
defaultMode = 1 #On first boot, use this mode. 0 is filter only, 1 is schedule mode, 2 is hold temp
inactivityTimeoutMinutes = 60
screenTimeoutMinutes = 5
tempCheckTimes = [0] #If minute is one of these times, check temperature in hold temp mode (format [0, 15, 30, 45])
sensorWarmupTime = 60 #Time in seconds. Sensor is mounted externally on this hot tub so it's going to take a while before it reflects the current temp
heaterCooldownTimeMinutes = 5 #Time in minutes. The high amperage heater will destroy its relay over time. It's best to cycle it on and off as infrequently as possible
timeWindowStart = 18 #Hours to run filter/heat cycle
timeWindowEnd = 20
temperatureUnit = 'F' #Uppercase F or C. Bad inputs will cause program to stop
minTemp = 60
maxTemp = 108 #Dont go crazy with these
maxTempSag = 0.2 #Prevent short cycles on the heater by not turning it on until it's this far below targetTemp. Only functions in Schedule and Manual modes
buttonType = 1 #0 is interrupt triggers, 1 is time-based polling
buttonBounceTime = 300 #Lower for better button response, raise to avoid accidental doubleclicks
enableWebOutput = 0
webOutputFile = "/dev/shm/hottubstatus" #This should be something in RAM to avoid excessive writes to the SD card
tempSensorAddress = "031722cbb8ff" #Check for this in /sys/bus/w1/devices/
##End user options##


lcd = I2C_LCD_driver.lcd() #Initialize LCD
tempSensor = W1ThermSensor(W1ThermSensor.THERM_SENSOR_DS18B20, tempSensorAddress) #Initialize temp sensor. Note that pin is probably set in /boot/config.txt. Kernel default is pin 4.


#Setup some variables here with initial values. These shouldn't need to be set by hand unless testing
runMode = defaultMode #Heating/filer schedule. 0 is filter only (summer mode), 1 is schedule mode, 2 is hold-temp mode. Note that this is the default mode after a power failure
manualMode = 0 #Manual mode overrides some settings while people are in the hot tub
targetTemp = 98 #Relatively safe default
turnOnTemp = targetTemp - maxTempSag
currentTemp = 0
pumpStatus = 0
heatStatus = 0
lightStatus = 0
blowerStatus = 0
buttonLedStatus = 0
inactivityTime = 0
inactivityTimeout = inactivityTimeoutMinutes * 60
screenTimeout = screenTimeoutMinutes * 60
heaterCooldownTime = heaterCooldownTimeMinutes * 60
heaterOffTime = 0
buttonPressTime = 0
loopProtect = 0
inTimeWindow = 0
watchingButton = 0
debounceTimer = 0
epochTime = 0
lcd.lcd_clear()


def getCurrentTime():
    #Time gets epoch time in seconds, localtime converts that into a tuple that can be used more easily
    etime = time.time()
    currentTime = time.localtime(etime) 
    return currentTime[3], currentTime[4], currentTime[5], etime

def readCurrentTemp():
    #Due to slow sensor reads slowing my main loop, I've split this off into a different thread that loops constantly, updating currentTemp when it's ready
    while True:
        global currentTemp
        if temperatureUnit == 'F':
            currentTemp = tempSensor.get_temperature(W1ThermSensor.DEGREES_F)
        elif temperatureUnit == 'C':
            currentTemp = tempSensor.get_temperature(W1ThermSensor.DEGREES_C)
        else:
            faultMode() #Note that this only kills the temp reading thread
        currentTemp = round(currentTemp, 1)
        time.sleep(1)
        #print(currentTemp)
        #return currentTemp


def filterOnlyMode():
    if heatStatus != 0: #Shut off heater if it's on
        heaterOff()
    if manualMode != 1:
        if (inTimeWindow == 1): #If within time window, run pump on low
            if pumpStatus == 0:
                pumpRunLow()
        else:
            if pumpStatus != 0:
                pumpOff()


def scheduleMode():
    #if manualMode != 1: #Moving the manual check to later in this function
    if (inTimeWindow == 1): #If within time window, run pump on low
        if pumpStatus == 0: #Note this isn't the usual != 1 here; we don't want to force low speed if someone has high speed going instead. This does prevent a user from stopping the pump during this time
            pumpRunLow()
        if manualMode != 1: #Manual mode does this already. No need to do it twice per loop
            if (epochTime - pumpStartTime >= sensorWarmupTime): #If sensor has had a chance to warm up, compare temps
                if debug:
                    print ("Sensor warm")
                if (currentTemp < turnOnTemp):
                    if heatStatus != 1:
                        heaterOn()
                elif (currentTemp >= targetTemp):
                    if heatStatus != 0:
                        heaterOff()
                #else: #Error catch, should never hit this
                #    if heatStatus != 0:
                #        heaterOff()
            elif debug:
                print ("Sensor not warmed up")
    elif manualMode != 1: #Dont shut off the pump if in manual mode
        if heatStatus != 0:
            heaterOff()
        if pumpStatus != 0:
            pumpOff()


def holdTempMode():
    global loopProtect
    if manualMode != 1: #Manual mode has its own temperature holding. The temp check intervals holdTemp mode uses are so close together it doesn't matter if a user skips one
        if (minute in tempCheckTimes or pumpStatus != 0): #If current time is one of the listed minutes OR if pump is already running check the temp. The pumpstatus check allows sensor warmups > 1 minute
            if debug:
                print("Check temperature now - loopProtect", loopProtect)
            if (pumpStatus != 1 and loopProtect == 0): #If pump is not on low and we haven't already started it before, turn it on
                pumpRunLow()
                loopProtect = 1 #Prevent pump from being restarted multiple times in the check period, since the reading could finish in less than 60 seconds
            if (epochTime - pumpStartTime >= sensorWarmupTime): #If sensor has had a chance to warm up, compare temps
                if debug:
                    print ("Sensor warm")
                if (currentTemp < targetTemp):
                    if (heatStatus != 1 and pumpStatus != 0):
                        heaterOn()
                else:
                    heaterOff()
                    pumpOff() #If up to temp after sensor caught up, shut everything off.
            elif debug:
                print ("Sensor not warmed up")
        else:
            pumpOff()
            loopProtect = 0 #Reset loop protection once outside of check period
            if debug:
                print ("Not time to check temperature")


def faultMode():
    heaterOff()
    pumpOff()
    blowerOff()
    lightOff()
    lcd.lcd_clear()
    lcd.lcd_display_string_pos("FAULT - STOPPING", 2, 2)
    lcd.backlight(0)
    print("FAULT - STOPPING")
    GPIO.cleanup()
    sys.exit(0)


def manualRunMode():
    global manualMode
    #inactivityTime = epochTime - buttonPressTime #Moving this to main loop
    if inactivityTime > inactivityTimeout: #If idle for too long, exit manual mode
        if debug:
            print ("Leaving manual mode due to inactivity")
        #Look into flashing some lights as a warning, or putting something on the LCD
        heaterOff()
        pumpOff()
        blowerOff()
        lightOff()
        manualMode = 0
    if (pumpStatus != 2 and blowerStatus == 0 and lightStatus == 0): #If user shuts everything off (with pump off or low), disable manual mode to resume normal functions
        if (pumpStatus == 0 or inTimeWindow == 1): #Pump on low is as "off" as it gets in a time window
            if debug:
                print ("Everything is off - Leaving manual mode")
            manualMode = 0
    if runMode != 0: #No heat in filter-only mode
        if pumpStatus != 0: #Dont do any heat related stuff if the pump is off. Remember that the blower and light will also trigger manual mode
            if (epochTime - pumpStartTime >= sensorWarmupTime): #If sensor has had a chance to warm up, compare temps
                if debug:
                    print ("Sensor warm")
                if (currentTemp < turnOnTemp):
                    if heatStatus != 1:
                        heaterOn()
                elif (currentTemp >= targetTemp):
                    if heatStatus != 0:
                        heaterOff()
                #else:
                #    if heatStatus != 0:
                #        heaterOff()
            elif debug:
                print ("Sensor not warmed up")
        else: #Safeguard to make sure the heater isnt running if the user stops the pump in manual mode
            if heatStatus != 0:
                heaterOff()

def pumpRunLow():
    GPIO.output(pumpLowPin, 0)
    GPIO.output(pumpHighPin, 1)
    global pumpStatus
    global pumpStartTime
    if pumpStatus == 0: #Only reset this if the pump was off
        pumpStartTime = epochTime
    pumpStatus = 1


def pumpRunHigh():
    #Shut off pumpLowPin, turn on pumpHighPin. I don't know what would happen if both were on, but let's not find out.
    GPIO.output(pumpLowPin, 1)
    GPIO.output(pumpHighPin, 0)
    global pumpStatus
    global pumpStartTime
    if pumpStatus == 0: #Only reset this if the pump was off
        pumpStartTime = epochTime
    pumpStatus = 2


def pumpOff():
    heaterOff() #Turn off heater before stopping pump
    GPIO.output(pumpLowPin, 1)
    GPIO.output(pumpHighPin, 1)
    global pumpStatus
    pumpStatus = 0


def heaterOn():
    heatTimer = epochTime - heaterOffTime
    if heatTimer > heaterCooldownTime:
        GPIO.output(heaterPin, 0)
        global heatStatus
        heatStatus = 1
        if debug:
            print("Heater on")
    elif debug:
        print("Heater in cooldown period", heatTimer)


def heaterOff():
    global heaterOffTime
    GPIO.output(heaterPin, 1)
    global heatStatus
    heaterOffTime = epochTime
    heatStatus = 0
    if debug:
        print("Heater off")


def blowerOn():
    GPIO.output(blowerPin, 0)
    global blowerStatus
    blowerStatus = 1


def blowerOff():
    GPIO.output(blowerPin, 1)
    global blowerStatus
    blowerStatus = 0


def lightOn():
    GPIO.output(lightPin, 0)
    global lightStatus
    lightStatus = 1


def lightOff():
    GPIO.output(lightPin, 1)
    global lightStatus
    lightStatus = 0


def buttonLedOn():
    GPIO.output(buttonLedPin, 0)
    #Do i even need a status here?
    global buttonLedStatus
    buttonLedStatus = 1


def buttonLedOff():
    GPIO.output(buttonLedPin, 1)
    global buttonLedStatus
    buttonLedStatus = 0


def buttonReader(pin): #buttonReader function determines which way to read the GPIO buttons based on user option
    buttonStatus = 0 #Reset this to 0
    if buttonType == 0: #Interrupt type button detection
        if GPIO.event_detected(pin):
            buttonStatus = 1
    elif buttonType == 1: #Time based polling button detection
        buttonStatus = pollButton(pin)
    return buttonStatus


def pollButton(pin): #This is the time-based polling GPIO read
    global debounceTimer #Need to make this a global to prevent it from being discarded
    global watchingButton #Same for this one
    buttonRead = GPIO.input(pin)
    if buttonRead == False: #Note inverted logic; we're pulling to ground when button is pressed
        if epochTime - debounceTimer > buttonBounceTime: #If pin pulled low for longer than BounceTime
            return True #Legitimate button press
        else:
            if watchingButton == 0: #If this is the first time the button press is noticed, mark the time
                debounceTimer = epochTime
            watchingButton = 1
    else: #Reset watchingButton if button is not currently pressed
        watchingButton = 0


def readButtons():
    global runMode
    global targetTemp
    global manualMode
    global buttonPressTime
    #Kill screen saver on first press of any button, without activating that button's function
    if buttonLedStatus == 0:
        if buttonReader(pumpButtonPin):
            buttonPressTime = epochTime #Note time for inactivity timer
            return 
        if buttonReader(blowerButtonPin):
            buttonPressTime = epochTime #Note time for inactivity timer
            return 
        if buttonReader(lightButtonPin):
            buttonPressTime = epochTime #Note time for inactivity timer
            return 
        if buttonReader(modeButtonPin):
            buttonPressTime = epochTime #Note time for inactivity timer
            return 
        if buttonReader(tempUpButtonPin):
            buttonPressTime = epochTime #Note time for inactivity timer
            return 
        if buttonReader(tempDownButtonPin):
            buttonPressTime = epochTime #Note time for inactivity timer
            return 
    #Pump
    if buttonReader(pumpButtonPin):
        buttonPressTime = epochTime #Note time for inactivity timer
        if debug:
            print ("Pump button pressed")
        manualMode = 1
        if pumpStatus == 0: #If pump is off, start it on low
            pumpRunLow()
        elif pumpStatus == 1: #If pump is on low, set it on high
            pumpRunHigh()
        else:
            if (runMode == 2 or inTimeWindow == 0): #If in hold temp mode, turn off. If in filter only or schedule mode but not in a time window, turn off
                pumpOff()
            else: #Revert to low speed if in schedule mode in a time window. Manual mode will exit on its own
                pumpRunLow()
    #Blower
    if buttonReader(blowerButtonPin):
        buttonPressTime = epochTime #Note time for inactivity timer
        if debug:
            print ("Blower button pressed")
        manualMode = 1
        if blowerStatus == 0:
            blowerOn()
        else:
            blowerOff()
    #Light
    if buttonReader(lightButtonPin):
        buttonPressTime = epochTime #Note time for inactivity timer
        if debug:
            print ("Light button pressed")
        manualMode = 1
        if lightStatus == 0:
            lightOn()
        else:
            lightOff()
    #Mode
    if buttonReader(modeButtonPin):
        if debug:
            print ("Mode button pressed")
        if runMode == 0:
            runMode = 1
        elif runMode == 1:
            runMode = 2
        else:
            runMode = 0
    #TempUp
    if buttonReader(tempUpButtonPin):
        if debug:
            print ("TempUp button pressed")
        if targetTemp < maxTemp:
            targetTemp = targetTemp + 1
            turnOnTemp = targetTemp - maxTempSag
    #TempDown
    if buttonReader(tempDownButtonPin):
        if debug:
            print ("TempDown button pressed")
        if targetTemp > minTemp:
            targetTemp = targetTemp - 1
            turnOnTemp = targetTemp - maxTempSag


def screenOutput():
    #Temperatures
    lcd.lcd_display_string_pos("Temp:     ", 1, 0)
    lcd.lcd_display_string_pos("{}".format(currentTemp), 1, 6)
    lcd.lcd_display_string_pos(" -> ", 1, 11) #target temp and heater status. Note blank spaces to overwrite old values
    lcd.lcd_display_string_pos("{} ".format(targetTemp), 1, 15) #The quoted braces and .format() allow variables to be subbed into string
    #Modes
    if runMode == 0:
        if manualMode == 1:
            lcd.lcd_display_string_pos("Manual - No Heat    ", 2, 0)
        else:
            lcd.lcd_display_string_pos("Filter Only         ", 2, 0)
        #time.sleep(5) #possibly switch this line back and forth with schedule. dont use sleep, do an epoch check
        #lcd.lcd_display_string_pos("<schedule>", 3, 0)
    elif runMode == 1:
        if manualMode == 1:
            lcd.lcd_display_string_pos("Schedule - Manual   ", 2, 0)
        else:
            lcd.lcd_display_string_pos("Schedule Mode       ", 2, 0)
        #time.sleep(5)
        #lcd.lcd_display_string_pos("<schedule>", 3, 0)
    elif runMode == 2:
        if manualMode == 1:
            lcd.lcd_display_string_pos("Hold Temp - Manual  ", 2, 0)
        else:
            lcd.lcd_display_string_pos("Hold Temp Mode      ", 2, 0)
    #Clock
    if hour < 10:
        lcd.lcd_display_string_pos("0{}:".format(hour), 4, 6)
    else:
        lcd.lcd_display_string_pos("{}:".format(hour), 4, 6)
    if minute < 10:
        lcd.lcd_display_string_pos("0{}:".format(minute), 4, 9)
    else:
        lcd.lcd_display_string_pos("{}:".format(minute), 4, 9)
    if second < 10:
        lcd.lcd_display_string_pos("0{}".format(second), 4, 12)
    else:
        lcd.lcd_display_string_pos("{}".format(second), 4, 12)
    #Outputs
    if pumpStatus == 0:
        lcd.lcd_display_string_pos("    ", 3, 0)
    elif pumpStatus == 1:
        lcd.lcd_display_string_pos("pump", 3, 0)
    elif pumpStatus == 2:
        lcd.lcd_display_string_pos("PUMP", 3, 0)
    if heatStatus == 0:
        lcd.lcd_display_string_pos("    ", 3, 5)
    elif heatStatus == 1:
        lcd.lcd_display_string_pos("HEAT", 3, 5)
    if blowerStatus == 0:
        lcd.lcd_display_string_pos("    ", 3, 10)
    elif blowerStatus == 1:
        lcd.lcd_display_string_pos("BLOW", 3, 10)
    if lightStatus == 0:
        #lcd.backlight(0) #Unfortunately this doesn't work. The backlight turns on any time the screen is updated
        lcd.lcd_display_string_pos("     ", 3, 15)
    elif lightStatus == 1:
        lcd.lcd_display_string_pos("LIGHT", 3, 15)
        #lcd.backlight(1)


def screenSaver():
    #Turn off LCD backlight
    lcd.backlight(0)
    #Turn off button LEDs
    buttonLedOff()


def outputToText():
    textfile = open(webOutputFile, 'w')
    #this needs to be rewritten. write only accepts one arg
    textfile.write(epochTime)
    textfile.close()


#Clear any previous GPIO config - May want to specify to only clear this program's pins in the future to play nice with others (though on my Pi 1 I'm using all but 3)
#Set up GPIO pins and interrupts. This uses the internal pull UP resistor, so we want the falling edge. The other pin of the buttons is GND
GPIO.cleanup()
GPIO.setmode(GPIO.BCM)
GPIO.setup(pumpLowPin, GPIO.OUT, initial=1)
GPIO.setup(pumpHighPin, GPIO.OUT, initial=1)
GPIO.setup(heaterPin, GPIO.OUT, initial=1)
GPIO.setup(blowerPin, GPIO.OUT, initial=1)
GPIO.setup(lightPin, GPIO.OUT, initial=1)
GPIO.setup(buttonLedPin, GPIO.OUT, initial=1)
GPIO.setup(pumpButtonPin , GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(blowerButtonPin , GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(lightButtonPin , GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(modeButtonPin , GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(tempUpButtonPin , GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(tempDownButtonPin , GPIO.IN, pull_up_down=GPIO.PUD_UP)
#Interrupts aren't really working. They false trigger on EVERYTHING. Crosstalk, EMI from the motors, etc.
#Interrupts left as user option, just in case
if buttonType == 0:
    GPIO.add_event_detect(pumpButtonPin, GPIO.FALLING, bouncetime=buttonBounceTime)
    GPIO.add_event_detect(blowerButtonPin, GPIO.FALLING, bouncetime=buttonBounceTime)
    GPIO.add_event_detect(lightButtonPin, GPIO.FALLING, bouncetime=buttonBounceTime)
    GPIO.add_event_detect(modeButtonPin, GPIO.FALLING, bouncetime=buttonBounceTime)
    GPIO.add_event_detect(tempUpButtonPin, GPIO.FALLING, bouncetime=buttonBounceTime)
    GPIO.add_event_detect(tempDownButtonPin, GPIO.FALLING, bouncetime=buttonBounceTime)

#Shut off all relays on initialization, redundant but just in case
#pumpOff()
#heaterOff()
#blowerOff()
#lightOff()


if temperatureUnit not in ['F', 'C']:
    faultMode()


#Begin sensor read thread
sensorThread = threading.Thread(target=readCurrentTemp)
sensorThread.setDaemon(True) #Daemonized threads die if main process is killed
sensorThread.start()


buttonLedOn()
#Begin main loop
try: #The try/catch should handle ctrl c more gracefully and allow me to cleanup the GPIO
    while True:
        currentTime = getCurrentTime()
        hour = currentTime[0]
        minute = currentTime[1]
        second = currentTime[2]
        epochTime = currentTime[3]
        inactivityTime = epochTime - buttonPressTime
        if debug:
            print ("Time is", hour, ":", minute, ":", second)
            #print ("Epoch", epochTime)
        if debug:
            print ("Temperature is", currentTemp)
            print ("Target Temp is", targetTemp)
        if (hour >= timeWindowStart and hour <= timeWindowEnd): #If within time window
            inTimeWindow = 1
        else:
            inTimeWindow = 0
        if debug:
            if (inTimeWindow == 1):
                print ("Within schedule window") 
            else:
                print ("Outside schedule window")
        if manualMode == 1:
            if debug:
                print ("Running in manual mode")
            manualRunMode()
        #Check run mode, do that stuff
        if runMode == 0:
            filterOnlyMode()
        elif runMode == 1:
            scheduleMode()
        elif runMode == 2:
            holdTempMode()
        else:
            faultMode()
        #Read button events
        readButtons()
        if inactivityTime < screenTimeout: #Only print to screen if screensaver mode is off
            if buttonLedStatus == 0:
                buttonLedOn()
            #Print status on LCD. Backlight turns on automatically
            screenOutput()
        else:
            if debug:
                print("Screensaver active")
            if buttonLedStatus == 1:
                screenSaver()
            time.sleep(.25) #Turns out it's the LCD commands that slow the loop down. Without printing to the screen this loops hundreds of times per second, pegging CPU
        if (pumpStatus == 0 and heatStatus != 0): #The heater should NEVER be on without the pump. Run this check at the end of each loop for debugging
            faultMode()
        if debug:
            print ("") #Newline for formatting
        if enableWebOutput:
            outputToText()
        #time.sleep(1) #This is helpful for some bench testing to keep loop speeds reasonable when there are no sensors to read
except KeyboardInterrupt:
        GPIO.cleanup()
