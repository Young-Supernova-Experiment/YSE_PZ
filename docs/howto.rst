Using the web interface
***********************

This page contains some general how-to's on using the :code:`YSE_PZ`
web interface.

Using the Admin interface
--------------------------

Most day-to-day actions (changing a status, requesting follow-up) are done
directly on the transient detail page and record who made the change
automatically. The Admin interface is only available to staff. When you
change or add a field there, make sure your username is selected in the
‘modified_by’ fields.  This will allow us to keep track of changes and ask
you questions if we’re confused by something.

Changing transient statuses
---------------------------
The status section of the Summary tab on the detail page for each object
has buttons for the common statuses (Following, Watch, Interesting, Ignore)
and a drop-down menu with all other statuses. Click a button to change the
status; no admin page is involved and your username is recorded
automatically.

If you changed the transient status to FollowupRequested or
Following, be sure to communicate with the slack channel to make sure everyone
knows what they should be doing to follow the object.
See Adding Followup Requests below.

Adding Followup Requests
------------------------
If you’ve labeled a transient as FollowupRequested, please use the
‘Add Transient Follow-up’ box on the Follow-up tab of the transient detail
page.  Select the classical, ToO or queued resource that you want to
use for the object followup, set the priority, and add a comment if needed.
Several people can request follow-up of the same object; each request is
listed separately under the follow-up entry.

If you want to add further instructions, you can
use the ‘Add Observation Task’ box, which lets you specify configurations,
exposure times, requested dates, etc.
Select ‘Requested’ as the status.

Recording Observations
----------------------

If you’ve recently observed something:

If no observation task exists, you can create an observation task using the
links in the followup section of the transient detail page.

If an observation task with status ‘requested’ exists, you can use the link in
the followup table to change the status to ‘successful’ and adjust the date
and other fields so that they’re an accurate record of the observation.

After recording the observation, make sure the transient status has been set to
Following using the status buttons on the Summary tab of the detail page.
