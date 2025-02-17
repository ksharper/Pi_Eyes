import cv2
import math
import pi3d
import random
import threading
import time
import RPi.GPIO as GPIO
import numpy as np
from svg.path import Path, parse_path
from xml.dom.minidom import parse
from gfxutil import *



# INPUT CONFIG for eye motion ----------------------------------------------
# ANALOG INPUTS REQUIRE SNAKE EYES BONNET

PUPIL_IN        = -1    # Analog input for pupil control (-1 = auto)
JOYSTICK_X_FLIP = False # If True, reverse stick X axis
JOYSTICK_Y_FLIP = False # If True, reverse stick Y axis
PUPIL_IN_FLIP   = False # If True, reverse reading from PUPIL_IN
TRACKING        = True  # If True, eyelid tracks pupil
PUPIL_SMOOTH    = 16    # If > 0, filter input from PUPIL_IN
PUPIL_MIN       = 0.0   # Lower analog range from PUPIL_IN
PUPIL_MAX       = 1.0   # Upper "
BLINK_PIN       = 0    # GPIO pin for blink button
AUTOBLINK       = True  # If True, eye blinks autonomously


# GPIO initialization ------------------------------------------------------

GPIO.setmode(GPIO.BCM)
if BLINK_PIN >= 0: GPIO.setup(BLINK_PIN , GPIO.IN, pull_up_down=GPIO.PUD_UP)



# Load SVG file, extract paths & convert to point lists --------------------

# Thanks Glen Akins for the symmetrical-lidded cyclops eye SVG!
# Iris & pupil have been scaled down slightly in this version to compensate
# for how the WorldEye distorts things...looks OK on WorldEye now but might
# seem small and silly if used with the regular OLED/TFT code.
dom               = parse("graphics/cyclops-eye.svg")
vb                = get_view_box(dom)
pupilMinPts       = get_points(dom, "pupilMin"      , 32, True , True )
pupilMaxPts       = get_points(dom, "pupilMax"      , 32, True , True )
irisPts           = get_points(dom, "iris"          , 32, True , True )
scleraFrontPts    = get_points(dom, "scleraFront"   ,  0, False, False)
scleraBackPts     = get_points(dom, "scleraBack"    ,  0, False, False)
upperLidClosedPts = get_points(dom, "upperLidClosed", 33, False, True )
upperLidOpenPts   = get_points(dom, "upperLidOpen"  , 33, False, True )
upperLidEdgePts   = get_points(dom, "upperLidEdge"  , 33, False, False)
lowerLidClosedPts = get_points(dom, "lowerLidClosed", 33, False, False)
lowerLidOpenPts   = get_points(dom, "lowerLidOpen"  , 33, False, False)
lowerLidEdgePts   = get_points(dom, "lowerLidEdge"  , 33, False, False)


# Set up display and initialize pi3d ---------------------------------------

DISPLAY = pi3d.Display.create(samples=4)
DISPLAY.set_background(0, 0, 0, 1) # r,g,b,alpha

# eyeRadius is the size, in pixels, at which the whole eye will be rendered.
if DISPLAY.width <= (DISPLAY.height * 2):
    # For WorldEye, eye size is -almost- full screen height
    eyeRadius   = DISPLAY.height / 2.1
else:
    eyeRadius   = DISPLAY.height * 2 / 5

# A 2D camera is used, mostly to allow for pixel-accurate eye placement,
# but also because perspective isn't really helpful or needed here, and
# also this allows eyelids to be handled somewhat easily as 2D planes.
# Line of sight is down Z axis, allowing conventional X/Y cartesion
# coords for 2D positions.
cam    = pi3d.Camera(is_3d=False, at=(0,0,0), eye=(0,0,-1000))
shader = pi3d.Shader("uv_light")
light  = pi3d.Light(lightpos=(0, -500, -500), lightamb=(0.2, 0.2, 0.2))


# Load texture maps --------------------------------------------------------

irisMap   = pi3d.Texture("graphics/iris.jpg"  , mipmap=False,
              filter=pi3d.constants.GL_LINEAR)
scleraMap = pi3d.Texture("graphics/sclera.png", mipmap=False,
              filter=pi3d.constants.GL_LINEAR, blend=True)
lidMap    = pi3d.Texture("graphics/lid.png"   , mipmap=False,
              filter=pi3d.constants.GL_LINEAR, blend=True)
# U/V map may be useful for debugging texture placement; not normally used
#uvMap     = pi3d.Texture("graphics/uv.png"    , mipmap=False,
#              filter=pi3d.constants.GL_LINEAR, blend=False, m_repeat=True)


# Initialize static geometry -----------------------------------------------

# Transform point lists to eye dimensions
scale_points(pupilMinPts      , vb, eyeRadius)
scale_points(pupilMaxPts      , vb, eyeRadius)
scale_points(irisPts          , vb, eyeRadius)
scale_points(scleraFrontPts   , vb, eyeRadius)
scale_points(scleraBackPts    , vb, eyeRadius)
scale_points(upperLidClosedPts, vb, eyeRadius)
scale_points(upperLidOpenPts  , vb, eyeRadius)
scale_points(upperLidEdgePts  , vb, eyeRadius)
scale_points(lowerLidClosedPts, vb, eyeRadius)
scale_points(lowerLidOpenPts  , vb, eyeRadius)
scale_points(lowerLidEdgePts  , vb, eyeRadius)

# Regenerating flexible object geometry (such as eyelids during blinks, or
# iris during pupil dilation) is CPU intensive, can noticably slow things
# down, especially on single-core boards.  To reduce this load somewhat,
# determine a size change threshold below which regeneration will not occur;
# roughly equal to 1/2 pixel, since 2x2 area sampling is used.

# Determine change in pupil size to trigger iris geometry regen
irisRegenThreshold = 0.0
a = points_bounds(pupilMinPts) # Bounds of pupil at min size (in pixels)
b = points_bounds(pupilMaxPts) # " at max size
maxDist = max(abs(a[0] - b[0]), abs(a[1] - b[1]), # Determine distance of max
              abs(a[2] - b[2]), abs(a[3] - b[3])) # variance around each edge
# maxDist is motion range in pixels as pupil scales between 0.0 and 1.0.
# 1.0 / maxDist is one pixel's worth of scale range.  Need 1/2 that...
if maxDist > 0: irisRegenThreshold = 0.5 / maxDist

# Determine change in eyelid values needed to trigger geometry regen.
# This is done a little differently than the pupils...instead of bounds,
# the distance between the middle points of the open and closed eyelid
# paths is evaluated, then similar 1/2 pixel threshold is determined.
upperLidRegenThreshold = 0.0
lowerLidRegenThreshold = 0.0
p1 = upperLidOpenPts[len(upperLidOpenPts) // 2]
p2 = upperLidClosedPts[len(upperLidClosedPts) // 2]
dx = p2[0] - p1[0]
dy = p2[1] - p1[1]
d  = dx * dx + dy * dy
if d > 0: upperLidRegenThreshold = 0.5 / math.sqrt(d)
p1 = lowerLidOpenPts[len(lowerLidOpenPts) // 2]
p2 = lowerLidClosedPts[len(lowerLidClosedPts) // 2]
dx = p2[0] - p1[0]
dy = p2[1] - p1[1]
d  = dx * dx + dy * dy
if d > 0: lowerLidRegenThreshold = 0.5 / math.sqrt(d)

# Generate initial iris mesh; vertex elements will get replaced on
# a per-frame basis in the main loop, this just sets up textures, etc.
iris = mesh_init((32, 4), (0, 0.5 / irisMap.iy), True, False)
iris.set_textures([irisMap])
iris.set_shader(shader)
irisZ = zangle(irisPts, eyeRadius)[0] * 0.99 # Get iris Z depth, for later

# Eyelid meshes are likewise temporary; texture coordinates are
# assigned here but geometry is dynamically regenerated in main loop.
upperEyelid = mesh_init((33, 5), (0, 0.5 / lidMap.iy), False, True)
upperEyelid.set_textures([lidMap])
upperEyelid.set_shader(shader)
lowerEyelid = mesh_init((33, 5), (0, 0.5 / lidMap.iy), False, True)
lowerEyelid.set_textures([lidMap])
lowerEyelid.set_shader(shader)

# Generate sclera for eye...start with a 2D shape for lathing...
angle1 = zangle(scleraFrontPts, eyeRadius)[1] # Sclera front angle
angle2 = zangle(scleraBackPts , eyeRadius)[1] # " back angle
aRange = 180 - angle1 - angle2
pts    = []

# ADD EXTRA INITIAL POINT because of some weird behavior with Pi3D and
# VideoCore VI with the Lathed shapes we make later. This adds a *tiny*
# ring of extra polygons that simply disappear on screen. It's not
# necessary on VC4, but not harmful either, so we just do it rather
# than try to be all clever.
ca, sa = pi3d.Utility.from_polar((90 - angle1) + aRange * 0.0001)
pts.append((ca * eyeRadius, sa * eyeRadius))

for i in range(24):
    ca, sa = pi3d.Utility.from_polar((90 - angle1) - aRange * i / 23)
    pts.append((ca * eyeRadius, sa * eyeRadius))

eye = pi3d.Lathe(path=pts, sides=64)
eye.set_textures([scleraMap])
eye.set_shader(shader)
re_axis(eye, 0.0)


# Init global stuff --------------------------------------------------------

mykeys = pi3d.Keyboard() # For capturing key presses

startX       = random.uniform(-30.0, 30.0)
n            = math.sqrt(900.0 - startX * startX)
startY       = random.uniform(-n, n)
destX        = startX
destY        = startY
curX         = startX
curY         = startY
moveDuration = random.uniform(0.075, 0.175)
holdDuration = random.uniform(0.1, 1.1)
startTime    = 0.0
isMoving     = False

frames        = 0
beginningTime = time.time()

eye.positionX(0.0)
iris.positionX(0.0)
upperEyelid.positionX(0.0)
upperEyelid.positionZ(-eyeRadius - 42)
lowerEyelid.positionX(0.0)
lowerEyelid.positionZ(-eyeRadius - 42)

currentPupilScale  =  0.5
prevPupilScale     = -1.0 # Force regen on first frame
prevUpperLidWeight = 0.5
prevLowerLidWeight = 0.5
prevUpperLidPts    = points_interp(upperLidOpenPts, upperLidClosedPts, 0.5)
prevLowerLidPts    = points_interp(lowerLidOpenPts, lowerLidClosedPts, 0.5)

ruRegen = True
rlRegen = True

timeOfLastBlink = 0.0
timeToNextBlink = 1.0
blinkState      = 0
blinkDuration   = 0.1
blinkStartTime  = 0

trackingPos = 0.3

# Assigning our static_back to None
previous_back = None
previous_x = 200
previous_y= 100
max_move = 3

# To capture video from webcam. 
video = cv2.VideoCapture(0)
# To use a video file as input 
# cap = cv2.VideoCapture('filename.mp4')


# Generate one frame of imagery
def frame(p):
    global previous_back, previous_x, previous_y, max_move
    global startX, startY, destX, destY, curX, curY
    global moveDuration, holdDuration, startTime, isMoving
    global frames
    global iris
    global pupilMinPts, pupilMaxPts, irisPts, irisZ
    global eye
    global upperEyelid, lowerEyelid
    global upperLidOpenPts, upperLidClosedPts, lowerLidOpenPts, lowerLidClosedPts
    global upperLidEdgePts, lowerLidEdgePts
    global prevUpperLidPts, prevLowerLidPts
    global prevUpperLidWeight, prevLowerLidWeight
    global prevPupilScale
    global irisRegenThreshold, upperLidRegenThreshold, lowerLidRegenThreshold
    global luRegen, llRegen, ruRegen, rlRegen
    global timeOfLastBlink, timeToNextBlink
    global blinkState
    global blinkDuration
    global blinkStartTime
    global trackingPos

    DISPLAY.loop_running()

    now = time.time()
    dt  = now - startTime

    frames += 1
#	if(now > beginningTime):
#		print(frames/(now-beginningTime))


    # Reading frame(image) from video
    check, frame = video.read()
 
    # Converting color image to gray_scale image
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
  
    # Converting gray scale image to GaussianBlur 
    # so that change can be find easily
    gray = cv2.GaussianBlur(gray, (21, 21), 0)
  
    # In first iteration we assign the value 
    # of static_back to our first frame
    if previous_back is None:
        previous_back = gray
  
    # Difference between static background 
    # and current frame(which is GaussianBlur)
    diff_frame = cv2.absdiff(previous_back, gray)
  
    # If change in between static background and
    # current frame is greater than 30 it will show white color(255)
    thresh_frame = cv2.threshold(diff_frame, 10, 255, cv2.THRESH_BINARY)[1]
    thresh_frame = cv2.dilate(thresh_frame, None, iterations = 2)
  
    # Finding contour of moving object
    cnts,_ = cv2.findContours(thresh_frame.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
  
    if cnts:
        prev_area = 0
        largest_countour = 0
        for contour in cnts:
            area = cv2.contourArea(contour)
            if area > prev_area:
                prev_area = area
                largest_countour = contour

        # (x, y, w, h) = cv2.boundingRect(largest_countour)
        # making green rectangle around the moving object
        #cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 3)

        #cnts = np.vstack(cnts)

        (x, y, w, h) = cv2.boundingRect(largest_countour)
        # making green rectangle around the moving object

        x_center = math.trunc(x + (w/2))
        y_center = math.trunc(y + (h/2))

        # Eye position from analog inputs
        x_center = x+(w/2)
        y_center = y+(h/2)
        # if x_center - previous_x > max_move:
        #     x_center = previous_x + max_move
        # if x_center - previous_x < max_move:
        #     x_center = previous_x - max_move
        # if y_center - previous_y > max_move:
        #     y_center = previous_y + max_move
        # if y_center - previous_y < max_move:
        #     y_center = previous_y - max_move

        print(x_center,y_center)

        curX = ((320 + x_center)/6) + 260
        curY = ((240 - y_center)/6)
 
        previous_x = x_center
        previous_y = y_center

    # Displaying image in gray_scale
#    cv2.imshow("Gray Frame", gray)
  
    # Displaying the difference in currentframe to
    # the staticframe(very first_frame)
#    cv2.imshow("Difference Frame", diff_frame)
  
    # Displaying the black and white image in which if
    # intensity difference greater than 30 it will appear white
#    cv2.imshow("Threshold Frame", thresh_frame)
  
    # Displaying color frame with contour of motion of object
#    cv2.imshow("Color Frame", frame)
  
    previous_back = gray

    # Regenerate iris geometry only if size changed by >= 1/4 pixel
    if abs(p - prevPupilScale) >= irisRegenThreshold:
        # Interpolate points between min and max pupil sizes
        interPupil = points_interp(pupilMinPts, pupilMaxPts, p)
        # Generate mesh between interpolated pupil and iris bounds
        mesh = points_mesh((None, interPupil, irisPts), 4, -irisZ, True)
        # Assign to both eyes
        leftIris.re_init(pts=mesh)
        rightIris.re_init(pts=mesh)
        prevPupilScale = p

    # Eyelid WIP

    if AUTOBLINK and (now - timeOfLastBlink) >= timeToNextBlink:
        timeOfLastBlink = now
        duration        = random.uniform(0.035, 0.06)
        if blinkStateLeft != 1:
            blinkStateLeft     = 1 # ENBLINK
            blinkStartTimeLeft = now
            blinkDurationLeft  = duration
        if blinkStateRight != 1:
            blinkStateRight     = 1 # ENBLINK
            blinkStartTimeRight = now
            blinkDurationRight  = duration
        timeToNextBlink = duration * 3 + random.uniform(0.0, 4.0)

    if blinkStateLeft: # Left eye currently winking/blinking?
        # Check if blink time has elapsed...
        if (now - blinkStartTimeLeft) >= blinkDurationLeft:
            # Yes...increment blink state, unless...
            if (blinkStateLeft == 1 and # Enblinking and...
                ((BLINK_PIN >= 0 and    # blink pin held, or...
                  GPIO.input(BLINK_PIN) == GPIO.LOW) or
                (WINK_L_PIN >= 0 and    # wink pin held
                  GPIO.input(WINK_L_PIN) == GPIO.LOW))):
                # Don't advance yet; eye is held closed
                pass
            else:
                blinkStateLeft += 1
                if blinkStateLeft > 2:
                    blinkStateLeft = 0 # NOBLINK
                else:
                    blinkDurationLeft *= 2.0
                    blinkStartTimeLeft = now
    else:
        if WINK_L_PIN >= 0 and GPIO.input(WINK_L_PIN) == GPIO.LOW:
            blinkStateLeft     = 1 # ENBLINK
            blinkStartTimeLeft = now
            blinkDurationLeft  = random.uniform(0.035, 0.06)

    if blinkStateRight: # Right eye currently winking/blinking?
        # Check if blink time has elapsed...
        if (now - blinkStartTimeRight) >= blinkDurationRight:
            # Yes...increment blink state, unless...
            if (blinkStateRight == 1 and # Enblinking and...
                ((BLINK_PIN >= 0 and    # blink pin held, or...
                  GPIO.input(BLINK_PIN) == GPIO.LOW) or
                (WINK_R_PIN >= 0 and    # wink pin held
                  GPIO.input(WINK_R_PIN) == GPIO.LOW))):
                # Don't advance yet; eye is held closed
                pass
            else:
                blinkStateRight += 1
                if blinkStateRight > 2:
                    blinkStateRight = 0 # NOBLINK
                else:
                    blinkDurationRight *= 2.0
                    blinkStartTimeRight = now
    else:
        if WINK_R_PIN >= 0 and GPIO.input(WINK_R_PIN) == GPIO.LOW:
            blinkStateRight     = 1 # ENBLINK
            blinkStartTimeRight = now
            blinkDurationRight  = random.uniform(0.035, 0.06)

    if BLINK_PIN >= 0 and GPIO.input(BLINK_PIN) == GPIO.LOW:
        duration = random.uniform(0.035, 0.06)
        if blinkStateLeft == 0:
            blinkStateLeft     = 1
            blinkStartTimeLeft = now
            blinkDurationLeft  = duration
        if blinkStateRight == 0:
            blinkStateRight     = 1
            blinkStartTimeRight = now
            blinkDurationRight  = duration

    if TRACKING:
        n = 0.4 - curY / 60.0
        if   n < 0.0: n = 0.0
        elif n > 1.0: n = 1.0
        trackingPos = (trackingPos * 3.0 + n) * 0.25
        if CRAZY_EYES:
            n = 0.4 - curYR / 60.0
            if   n < 0.0: n = 0.0
            elif n > 1.0: n = 1.0
            trackingPosR = (trackingPosR * 3.0 + n) * 0.25

    if blinkStateLeft:
        n = (now - blinkStartTimeLeft) / blinkDurationLeft
        if n > 1.0: n = 1.0
        if blinkStateLeft == 2: n = 1.0 - n
    else:
        n = 0.0
    newLeftUpperLidWeight = trackingPos + (n * (1.0 - trackingPos))
    newLeftLowerLidWeight = (1.0 - trackingPos) + (n * trackingPos)

    if blinkStateRight:
        n = (now - blinkStartTimeRight) / blinkDurationRight
        if n > 1.0: n = 1.0
        if blinkStateRight == 2: n = 1.0 - n
    else:
        n = 0.0
    if CRAZY_EYES:
        newRightUpperLidWeight = trackingPosR + (n * (1.0 - trackingPosR))
        newRightLowerLidWeight = (1.0 - trackingPosR) + (n * trackingPosR)
    else:
        newRightUpperLidWeight = trackingPos + (n * (1.0 - trackingPos))
        newRightLowerLidWeight = (1.0 - trackingPos) + (n * trackingPos)

    if (luRegen or (abs(newLeftUpperLidWeight - prevLeftUpperLidWeight) >=
      upperLidRegenThreshold)):
        newLeftUpperLidPts = points_interp(upperLidOpenPts,
          upperLidClosedPts, newLeftUpperLidWeight)
        if newLeftUpperLidWeight > prevLeftUpperLidWeight:
            leftUpperEyelid.re_init(pts=points_mesh(
              (upperLidEdgePts, prevLeftUpperLidPts,
              newLeftUpperLidPts), 5, 0, False))
        else:
            leftUpperEyelid.re_init(pts=points_mesh(
              (upperLidEdgePts, newLeftUpperLidPts,
              prevLeftUpperLidPts), 5, 0, False))
        prevLeftUpperLidPts    = newLeftUpperLidPts
        prevLeftUpperLidWeight = newLeftUpperLidWeight
        luRegen = True
    else:
        luRegen = False

    if (llRegen or (abs(newLeftLowerLidWeight - prevLeftLowerLidWeight) >=
      lowerLidRegenThreshold)):
        newLeftLowerLidPts = points_interp(lowerLidOpenPts,
          lowerLidClosedPts, newLeftLowerLidWeight)
        if newLeftLowerLidWeight > prevLeftLowerLidWeight:
            leftLowerEyelid.re_init(pts=points_mesh(
              (lowerLidEdgePts, prevLeftLowerLidPts,
              newLeftLowerLidPts), 5, 0, False))
        else:
            leftLowerEyelid.re_init(pts=points_mesh(
              (lowerLidEdgePts, newLeftLowerLidPts,
              prevLeftLowerLidPts), 5, 0, False))
        prevLeftLowerLidWeight = newLeftLowerLidWeight
        prevLeftLowerLidPts    = newLeftLowerLidPts
        llRegen = True
    else:
        llRegen = False

    if (ruRegen or (abs(newRightUpperLidWeight - prevRightUpperLidWeight) >=
      upperLidRegenThreshold)):
        newRightUpperLidPts = points_interp(upperLidOpenPts,
          upperLidClosedPts, newRightUpperLidWeight)
        if newRightUpperLidWeight > prevRightUpperLidWeight:
            rightUpperEyelid.re_init(pts=points_mesh(
              (upperLidEdgePts, prevRightUpperLidPts,
              newRightUpperLidPts), 5, 0, True))
        else:
            rightUpperEyelid.re_init(pts=points_mesh(
              (upperLidEdgePts, newRightUpperLidPts,
              prevRightUpperLidPts), 5, 0, True))
        prevRightUpperLidWeight = newRightUpperLidWeight
        prevRightUpperLidPts    = newRightUpperLidPts
        ruRegen = True
    else:
        ruRegen = False

    if (rlRegen or (abs(newRightLowerLidWeight - prevRightLowerLidWeight) >=
      lowerLidRegenThreshold)):
        newRightLowerLidPts = points_interp(lowerLidOpenPts,
          lowerLidClosedPts, newRightLowerLidWeight)
        if newRightLowerLidWeight > prevRightLowerLidWeight:
            rightLowerEyelid.re_init(pts=points_mesh(
              (lowerLidEdgePts, prevRightLowerLidPts,
              newRightLowerLidPts), 5, 0, True))
        else:
            rightLowerEyelid.re_init(pts=points_mesh(
              (lowerLidEdgePts, newRightLowerLidPts,
              prevRightLowerLidPts), 5, 0, True))
        prevRightLowerLidWeight = newRightLowerLidWeight
        prevRightLowerLidPts    = newRightLowerLidPts
        rlRegen = True
    else:
        rlRegen = False

    convergence = 2.0

    # Right eye (on screen left)
    if CRAZY_EYES:
        rightIris.rotateToX(curYR)
        rightIris.rotateToY(curXR - convergence)
        rightIris.draw()
        rightEye.rotateToX(curYR)
        rightEye.rotateToY(curXR - convergence)
    else:
        rightIris.rotateToX(curY)
        rightIris.rotateToY(curX - convergence)
        rightIris.draw()
        rightEye.rotateToX(curY)
        rightEye.rotateToY(curX - convergence)
    rightEye.draw()

    # Left eye (on screen right)

    leftIris.rotateToX(curY)
    leftIris.rotateToY(curX + convergence)
    leftIris.draw()
    leftEye.rotateToX(curY)
    leftEye.rotateToY(curX + convergence)
    leftEye.draw()

    leftUpperEyelid.draw()
    leftLowerEyelid.draw()
    rightUpperEyelid.draw()
    rightLowerEyelid.draw()

    k = mykeys.read()
    if k==27:
        mykeys.close()
        DISPLAY.stop()
        exit(0)

def split( # Recursive simulated pupil response when no analog sensor
  startValue, # Pupil scale starting value (0.0 to 1.0)
  endValue,   # Pupil scale ending value (")
  duration,   # Start-to-end time, floating-point seconds
  range):     # +/- random pupil scale at midpoint
    startTime = time.time()
    if range >= 0.125: # Limit subdvision count, because recursion
        duration *= 0.5 # Split time & range in half for subdivision,
        range    *= 0.5 # then pick random center point within range:
        midValue  = ((startValue + endValue - range) * 0.5 +
                     random.uniform(0.0, range))
        split(startValue, midValue, duration, range)
        split(midValue  , endValue, duration, range)
    else: # No more subdivisons, do iris motion...
        dv = endValue - startValue
        while True:
            dt = time.time() - startTime
            if dt >= duration: break
            v = startValue + dv * dt / duration
            if   v < PUPIL_MIN: v = PUPIL_MIN
            elif v > PUPIL_MAX: v = PUPIL_MAX
            frame(v) # Draw frame w/interim pupil scale value


# MAIN LOOP -- runs continuously -------------------------------------------

while True:
    v = random.random()
    split(currentPupilScale, v, 4.0, 1.0)
    currentPupilScale = v
