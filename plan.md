# Plan
This is the roadmap to build an EHR simulator to evaluate the effect of AI assistance on the assements and decisions made by health care providers. 

Gist: the EHR simulator should replay historical timeseries from existing data, giving a simulated EHR view to the health care provider at a specific point in time. The EHR simulator should also be able to display the output of AI models. The simulator should be able to prompt the provider to answer specific questions on certain timepoints. 

## Data input
- historical timeseries of multiple data points (labs, vitals, imaging, historical data) over time
    - paths to example data is in .EXAMPLE_DATA_PATHS
    - should be modular to be able to accept differently formated data
- precomputed AI model output for each timepoint
- settings file: at what timepoints the clinician should look at each patients file, unit of timepoints (minutes, hours), list of patient ids that the clinician will have to go through
    - timepoints are defined relatively to the first contact with the patient (timepoint 0)
- questions file: questions that the clinician will have to answer at each timepoint 

## Interface
- Reproduce a familiar EHR interface for the clinician
- should be browser based
- physicians identifies with simple login: clinician_name
- a single patient displayed at a time
- all data up to the current (and including the) timepoint should be displayed
- should have tabs for admission data, vitals, labs, imaging, AI model
- should have header with patient id, age, sex
- questions pane: display questions for clinician to answer for that timepoint 
    - questions from a timepoint have to be answered before moving on to next timepoint
- simulator commands: go to next timepoint (questions have to be answered first), switch patient

## Output
- maintain simple database with answered questions per patient_id / clinician_name / timepoint 
- should easily export to csv file with columns: patient_id, clinician_name, timepoint, question_1, question_2 ... 

## Example work-flow
1. clinician (dr_john) identifies to web interface
2. EHR interface displays patient_1 at timepoint 0
3. dr_john navigates through timepoint 0 of patient_1
4. dr_john answers questions for patient_1 at timepoint 0
5. dr_john presses go to next timepoint button
6. dr_john navigates through timepoint 1 of patient_1
4. dr_john answers questions for patient_1 at timepoint 1
5. dr_john presses go to next timepoint button

etc...

## Second phase
- add possibility to randomize physician-patient pairs to with vs without AI assistance
    - in one arm, AI model output is displayed / in the other it is not
    - meta data to answer to question should contain if it was answered with or without assistance

## What's not needed
- model will be run in a local environment, so safety features are not needed for now
- this EHR interface will not host the AI model on it's own, but will receive AI model output and display it

## References
- https://doi.org/10.1038/s41591-026-04252-6 
- https://doi.org/10.1038/s43856-024-00666-w