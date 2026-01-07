from py4web import action, request, response
from .MyWubook import MyWubook
from datetime import datetime


@action("index")
def index():
    return "Hello World"


@action("api/get_rooms_between/{date_in}/{date_out}")
def get_rooms_between(date_in, date_out):
    """_summary_

    Args:
        date_in (str): 22-10-2025
        date_out (str): _description_
    """
    date_in_dt = datetime.strptime(date_in, "%d-%m-%Y")
    date_out_dt = datetime.strptime(date_out, "%d-%m-%Y")
    with MyWubook() as server:
        data = server.get_rooms_between(date_in_dt, date_out_dt)
    return data

@action("api/new_reservation", method=["POST"])
def new_reservation():
    pass