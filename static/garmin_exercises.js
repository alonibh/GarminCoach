/**
 * Garmin Connect supported strength exercise names, grouped by muscle group.
 * Used by the Plan page exercise editor for the searchable grouped dropdown.
 */
const GARMIN_EXERCISES = {
  "Chest": [
    "Bench Press", "Incline Bench Press", "Decline Bench Press",
    "Dumbbell Bench Press", "Incline Dumbbell Press", "Decline Dumbbell Press",
    "Push Up", "Wide Push Up", "Diamond Push Up", "Decline Push Up", "Incline Push Up",
    "Chest Fly", "Dumbbell Fly", "Cable Fly", "Cable Crossover", "Pec Deck",
    "Chest Dip", "Close Grip Bench Press",
  ],
  "Back": [
    "Pull Up", "Chin Up", "Wide Grip Pull Up",
    "Lat Pulldown", "Close Grip Lat Pulldown",
    "Bent Over Row", "Dumbbell Row", "T Bar Row", "Seated Cable Row", "Seated Row",
    "Face Pull", "Deadlift", "Romanian Deadlift", "Straight Leg Deadlift", "Rack Pull",
    "Good Morning", "Hyperextension", "Superman",
    "Reverse Fly", "Shrug", "Dumbbell Shrug", "Barbell Shrug",
  ],
  "Shoulders": [
    "Overhead Press", "Barbell Overhead Press", "Dumbbell Shoulder Press",
    "Arnold Press", "Seated Dumbbell Shoulder Press",
    "Lateral Raise", "Front Raise", "Rear Delt Fly", "Cable Lateral Raise",
    "Upright Row", "Cable Upright Row", "Face Pull", "Push Press",
  ],
  "Biceps": [
    "Bicep Curl", "Barbell Curl", "Dumbbell Curl", "Hammer Curl",
    "Incline Dumbbell Curl", "Concentration Curl", "Cable Curl",
    "Preacher Curl", "Reverse Curl", "Zottman Curl",
  ],
  "Triceps": [
    "Tricep Dip", "Tricep Pushdown", "Cable Tricep Pushdown",
    "Overhead Tricep Extension", "Skull Crusher", "Close Grip Bench Press",
    "Tricep Kickback", "Rope Pushdown", "Diamond Push Up",
  ],
  "Legs - Quads": [
    "Squat", "Back Squat", "Front Squat", "Goblet Squat", "Sumo Squat",
    "Leg Press", "Lunge", "Reverse Lunge", "Walking Lunge",
    "Bulgarian Split Squat", "Step Up", "Leg Extension",
    "Wall Sit", "Hack Squat", "Sissy Squat",
  ],
  "Legs - Hamstrings & Glutes": [
    "Romanian Deadlift", "Leg Curl", "Seated Leg Curl", "Lying Leg Curl",
    "Hip Thrust", "Barbell Hip Thrust", "Glute Bridge", "Single Leg Hip Thrust",
    "Cable Kickback", "Donkey Kick", "Good Morning", "Nordic Hamstring Curl",
  ],
  "Legs - Calves": [
    "Calf Raise", "Standing Calf Raise", "Seated Calf Raise",
    "Donkey Calf Raise", "Single Leg Calf Raise",
  ],
  "Core - Abs": [
    "Crunch", "Sit Up", "Decline Crunch", "Cable Crunch",
    "Hanging Leg Raise", "Hanging Knee Raise", "Leg Raise", "Knee Raise",
    "V Up", "Bicycle Crunch", "Ab Wheel Rollout", "Dragon Flag",
    "Toes To Bar", "Russian Twist", "Windmill",
  ],
  "Core - Plank & Stability": [
    "Plank", "Side Plank", "Reverse Plank", "Hollow Hold",
    "Dead Bug", "Bird Dog", "Swiss Ball Crunch", "Swiss Ball Rollout",
  ],
  "Core - Obliques": [
    "Oblique Crunch", "Side Bend", "Cable Wood Chop", "Russian Twist", "Pallof Press",
  ],
  "Full Body / Compound": [
    "Deadlift", "Power Clean", "Hang Clean", "Clean And Press", "Snatch",
    "Thruster", "Kettlebell Swing", "Kettlebell Clean", "Kettlebell Snatch",
    "Burpee", "Man Maker", "Turkish Get Up",
  ],
  "Cardio / Conditioning": [
    "Box Jump", "Jump Squat", "Jump Lunge", "Mountain Climber",
    "Jumping Jack", "High Knees", "Battle Ropes", "Sled Push", "Sled Pull",
  ],
};

// Flat deduplicated list for datalist/autocomplete
const GARMIN_EXERCISES_FLAT = [...new Set(Object.values(GARMIN_EXERCISES).flat())];
