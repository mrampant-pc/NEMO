from datetime import datetime, timedelta
from logging import getLogger
from typing import List

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods

from NEMO.decorators import user_office_or_manager_required
from NEMO.forms import TemporaryPhysicalAccessRequestForm
from NEMO.models import (
	Notification,
	PhysicalAccessLevel,
	RequestStatus,
	TemporaryPhysicalAccess,
	TemporaryPhysicalAccessRequest,
	User,
)
from NEMO.utilities import (
	BasicDisplayTable,
	EmailCategory,
	beginning_of_the_day,
	bootstrap_primary_color,
	export_format_datetime,
	format_datetime,
	get_full_url,
	quiet_int,
	render_email_template,
	send_mail,
)
from NEMO.views.customization import (
	ApplicationCustomization,
	CustomizationBase,
	EmailsCustomization,
	UserRequestsCustomization,
	get_media_file_contents,
)
from NEMO.views.notifications import create_access_request_notification, delete_notification, get_notifications

access_request_logger = getLogger(__name__)


@login_required
@require_GET
def access_requests(request):
	mark_requests_expired()
	user: User = request.user
	max_requests = quiet_int(UserRequestsCustomization.get("access_requests_display_max"), None)
	physical_access_requests = TemporaryPhysicalAccessRequest.objects.filter(deleted=False)
	physical_access_requests = physical_access_requests.order_by("-end_time")
	if not user.is_facility_manager and not user.is_staff and not user.is_user_office:
		# For some reason doing an "or" filtering with manytomany field returns duplicates, and using distinct() returns nothing...
		other_user_physical_access_requests = physical_access_requests.filter(other_users__in=[user]).distinct()
		physical_access_requests = physical_access_requests.filter(
			Q(creator=user) | Q(id__in=other_user_physical_access_requests)
		)
	dictionary = {
		"pending_access_requests": physical_access_requests.filter(status=RequestStatus.PENDING).order_by("start_time"),
		"approved_access_requests": physical_access_requests.filter(status=RequestStatus.APPROVED)[:max_requests],
		"denied_access_requests": physical_access_requests.filter(status=RequestStatus.DENIED)[:max_requests],
		"expired_access_requests": physical_access_requests.filter(status=RequestStatus.EXPIRED)[:max_requests],
		"access_requests_description": UserRequestsCustomization.get("access_requests_description"),
		"access_request_notifications": get_notifications(
			request.user, Notification.Types.TEMPORARY_ACCESS_REQUEST, delete=not user.is_facility_manager
		),
	}
	return render(request, "requests/access_requests/access_requests.html", dictionary)


@login_required
@require_http_methods(["GET", "POST"])
def create_access_request(request, request_id=None):
	user: User = request.user
	try:
		access_request = TemporaryPhysicalAccessRequest.objects.get(id=request_id)
	except TemporaryPhysicalAccessRequest.DoesNotExist:
		access_request = None

	dictionary = {
		"physical_access_levels": PhysicalAccessLevel.objects.filter(allow_user_request=True),
		"other_users": User.objects.filter(is_active=True).exclude(id=user.id),
	}

	if request.method == "POST":
		# some extra validation needs to be done here because it depends on the user
		edit = bool(access_request)
		errors = []
		if edit:
			if access_request.deleted:
				errors.append("You are not allowed to edit expired or deleted requests.")
			if access_request.status != RequestStatus.PENDING:
				errors.append("Only pending requests can be modified.")
			if access_request.creator != user and not user.is_facility_manager:
				errors.append("You are not allowed to edit a request you didn't create.")

		form = TemporaryPhysicalAccessRequestForm(
			request.POST, instance=access_request, initial={"creator": access_request.creator if edit else user}
		)

		# add errors to the form for better display
		for error in errors:
			form.add_error(None, error)

		cleaned_data = form.clean()
		if cleaned_data and not user.is_facility_manager and cleaned_data.get("start_time") < timezone.now():
			form.add_error("start_time", "The start time must be later than the current time")

		if form.is_valid():
			if not edit:
				form.instance.creator = user
			if edit and user.is_facility_manager:
				decision = [state for state in ["approve_request", "deny_request"] if state in request.POST]
				if decision:
					if next(iter(decision)) == "approve_request":
						access_request.status = RequestStatus.APPROVED
						create_temporary_access(access_request)
					else:
						access_request.status = RequestStatus.DENIED
					access_request.reviewer = user

			form.instance.last_updated_by = user
			new_access_request = form.save()
			create_access_request_notification(new_access_request)
			if edit:
				# remove notification for current user and other facility managers
				delete_notification(Notification.Types.TEMPORARY_ACCESS_REQUEST, new_access_request.id, [user])
				if user.is_facility_manager:
					managers = User.objects.filter(is_active=True, is_facility_manager=True)
					delete_notification(Notification.Types.TEMPORARY_ACCESS_REQUEST, new_access_request.id, managers)
			send_request_received_email(request, new_access_request, edit)
			return redirect("user_requests", "access")
		else:
			dictionary["form"] = form
			return render(request, "requests/access_requests/access_request.html", dictionary)
	else:
		form = TemporaryPhysicalAccessRequestForm(instance=access_request)
		dictionary["form"] = form
		return render(request, "requests/access_requests/access_request.html", dictionary)


@login_required
@require_GET
def delete_access_request(request, request_id):
	access_request = get_object_or_404(TemporaryPhysicalAccessRequest, id=request_id)

	if access_request.creator != request.user:
		return HttpResponseBadRequest("You are not allowed to delete a request you didn't create.")
	if access_request and access_request.status != RequestStatus.PENDING:
		return HttpResponseBadRequest("You are not allowed to delete a request that is not pending.")

	access_request.deleted = True
	access_request.save(update_fields=["deleted"])
	delete_notification(Notification.Types.TEMPORARY_ACCESS_REQUEST, access_request.id)
	return redirect("user_requests", "access")


def create_temporary_access(access_request: TemporaryPhysicalAccessRequest):
	for user in access_request.creator_and_other_users():
		TemporaryPhysicalAccess.objects.create(
			user=user,
			physical_access_level=access_request.physical_access_level,
			start_time=access_request.start_time,
			end_time=access_request.end_time,
		)


def mark_requests_expired():
	for expired_request in TemporaryPhysicalAccessRequest.objects.filter(
		status=RequestStatus.PENDING, deleted=False, end_time__lt=timezone.now()
	):
		delete_notification(Notification.Types.TEMPORARY_ACCESS_REQUEST, expired_request.id)
		expired_request.status = RequestStatus.EXPIRED
		expired_request.save(update_fields=["status"])


def send_request_received_email(request, access_request: TemporaryPhysicalAccessRequest, edit):
	user_office_email = EmailsCustomization.get("user_office_email_address")
	access_request_notification_email = get_media_file_contents("access_request_notification_email.html")
	if user_office_email and access_request_notification_email:
		# cc facility managers
		cc_users: List[User] = list(User.objects.filter(is_active=True, is_facility_manager=True))
		# and other users
		cc_users.extend(access_request.other_users.all())
		ccs = [
			email
			for user in cc_users
			for email in user.get_emails(user.get_preferences().email_send_access_request_updates)
		]
		ccs.append(user_office_email)
		status = (
			"approved"
			if access_request.status == RequestStatus.APPROVED
			else "denied"
			if access_request.status == RequestStatus.DENIED
			else "updated"
			if edit
			else "received"
		)
		absolute_url = get_full_url(reverse("user_requests", kwargs={"tab": "access"}), request)
		color_type = "success" if status == "approved" else "danger" if status == "denied" else "info"
		message = render_email_template(
			access_request_notification_email,
			{
				"template_color": bootstrap_primary_color(color_type),
				"access_request": access_request,
				"status": status,
				"access_requests_url": absolute_url,
			},
		)
		email_notification = access_request.creator.get_preferences().email_send_access_request_updates
		send_mail(
			subject=f"Your access request for the {access_request.physical_access_level.area} has been {status}",
			content=message,
			from_email=user_office_email,
			to=access_request.creator.get_emails(email_notification),
			cc=ccs,
			email_category=EmailCategory.ACCESS_REQUESTS,
		)


@login_required
@permission_required("NEMO.trigger_timed_services", raise_exception=True)
@require_GET
def email_weekend_access_notification(request):
	return send_email_weekend_access_notification()


def send_email_weekend_access_notification():
	"""
	Sends a weekend access email to the addresses set in customization with the template provided.
	The email is sent when the first request (each week) that includes weekend access is approved.
	If no weekend access requests are made by the given time on the cutoff day (if set), a no access email is sent.
	"""
	try:
		user_office_email = EmailsCustomization.get("user_office_email_address")
		email_to = UserRequestsCustomization.get("weekend_access_notification_emails")
		access_contents = get_media_file_contents("weekend_access_email.html")
		if user_office_email and email_to and access_contents:
			process_weekend_access_notification(user_office_email, email_to, access_contents)
	except Exception as error:
		access_request_logger.error(error)
	return HttpResponse()


def process_weekend_access_notification(user_office_email, email_to, access_contents):
	today = datetime.today()
	beginning_of_the_week = beginning_of_the_day(today - timedelta(days=today.weekday()))
	cutoff_day = UserRequestsCustomization.get("weekend_access_notification_cutoff_day")
	cutoff_hour = UserRequestsCustomization.get("weekend_access_notification_cutoff_hour")
	# Set the cutoff in actual datetime format
	cutoff_datetime = None
	if cutoff_hour.isdigit() and cutoff_day and cutoff_day.isdigit():
		cutoff_datetime = (beginning_of_the_week + timedelta(days=int(cutoff_day))).replace(hour=int(cutoff_hour))

	end_of_the_week = beginning_of_the_week + timedelta(weeks=1)
	beginning_of_the_weekend = beginning_of_the_week + timedelta(days=5)

	# Approved access request that include weekend time do overlap with weekend date interval.
	approved_weekend_access_requests = TemporaryPhysicalAccessRequest.objects.filter(
		deleted=False, status=RequestStatus.APPROVED
	)
	approved_weekend_access_requests = approved_weekend_access_requests.exclude(start_time__gte=end_of_the_week)
	approved_weekend_access_requests = approved_weekend_access_requests.exclude(end_time__lte=beginning_of_the_weekend)

	cutoff_time_passed = cutoff_datetime and timezone.now() >= cutoff_datetime
	last_sent = CustomizationBase.get("weekend_access_notification_last_sent")
	last_sent_datetime = parse_datetime(last_sent) if last_sent else None
	if (
			(not last_sent_datetime or last_sent_datetime < beginning_of_the_week)
			and access_contents
			and approved_weekend_access_requests.exists()
			and not cutoff_time_passed
	):
		send_weekend_email_access(True, user_office_email, email_to, access_contents, beginning_of_the_week)
		CustomizationBase.set("weekend_access_notification_last_sent", str(timezone.now()))
	if access_contents and cutoff_datetime and not approved_weekend_access_requests.exists():
		is_cutoff = today.weekday() == int(cutoff_day) and cutoff_datetime.hour == timezone.localtime().hour
		if is_cutoff:
			send_weekend_email_access(False, user_office_email, email_to, access_contents, beginning_of_the_week)


def send_weekend_email_access(access, user_office_email, email_to, contents, beginning_of_the_week):
	facility_name = ApplicationCustomization.get("facility_name")
	recipients = tuple([e for e in email_to.split(",") if e])
	ccs = [
		email
		for manager in User.objects.filter(is_active=True, is_facility_manager=True)
		for email in manager.get_emails(manager.get_preferences().email_send_access_request_updates)
	]
	ccs.append(user_office_email)

	sat = format_datetime(beginning_of_the_week + timedelta(days=5), "SHORT_DATE_FORMAT", as_current_timezone=False)
	sun = format_datetime(beginning_of_the_week + timedelta(days=6), "SHORT_DATE_FORMAT", as_current_timezone=False)

	subject = f"{'NO w' if not access else 'W'}eekend access for the {facility_name} {sat} - {sun}"
	message = render_email_template(contents, {"weekend_access": access})
	send_mail(
		subject=subject,
		content=message,
		from_email=user_office_email,
		to=recipients,
		cc=ccs,
		email_category=EmailCategory.ACCESS_REQUESTS,
	)


@user_office_or_manager_required
@require_GET
def csv_export(request):
	return access_csv_export(TemporaryPhysicalAccessRequest.objects.filter(deleted=False))


def access_csv_export(request_list: List[TemporaryPhysicalAccessRequest]) -> HttpResponse:
	table_result = BasicDisplayTable()
	table_result.add_header(("status", "Status"))
	table_result.add_header(("created_date", "Created date"))
	table_result.add_header(("last_updated", "Last updated"))
	table_result.add_header(("creator", "Creator"))
	table_result.add_header(("other_users", "Buddies"))
	table_result.add_header(("area", "Area"))
	table_result.add_header(("access_level", "Access level"))
	table_result.add_header(("start", "Start"))
	table_result.add_header(("end", "End"))
	table_result.add_header(("reviewer", "Reviewer"))
	for req in request_list:
		req: TemporaryPhysicalAccessRequest = req
		table_result.add_row(
			{
				"status": req.get_status_display(),
				"created_date": req.creation_time,
				"last_updated": req.last_updated,
				"creator": req.creator,
				"other_users": ", ".join([str(user) for user in req.other_users.all()]),
				"area": req.physical_access_level.area,
				"access_level": req.physical_access_level,
				"start": req.start_time,
				"end": req.end_time,
				"reviewer": req.reviewer,
			}
		)

	name = slugify(UserRequestsCustomization.get("access_requests_title")).replace("-", "_")
	filename = f"{name}_{export_format_datetime()}.csv"
	response = table_result.to_csv()
	response["Content-Disposition"] = f'attachment; filename="{filename}"'
	return response
