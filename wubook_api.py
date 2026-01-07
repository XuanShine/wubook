# version 1.0
import os, sys
import pickle
import time
from dataclasses import dataclass
from datetime import date
C = os.path.abspath(os.path.dirname(__file__))
sys.path.append(C)

from datetime import datetime, timedelta
import xmlrpc.client
from loguru import logger
from dotenv import load_dotenv
load_dotenv()



### ROOM COFIGURATION ###

# différencier chambres "réelles" et chambres "virtuelles"
REAL_ROOMS = {"329039", "329667", "329670", "407751", "469743", "469744"}

type_room = {"329039": "double economic",
             "329667": "double balcony",
             "329670": "triple economic",
             "405126": "single balcony",
             "405127": "single economic",
             "407751": "triple balcony",
             "469743": "familiale",
             "469744": "kichenette"
            }

room_to_code = {"sstd": "405127",
                "sblc": "405126",
                "2C": "329039",
                "dblc": "329667",
                "tstd": "329670",
                "tblc": "407751",
                "fblc": "469743",
                "ktch": "469744"
               }


url = "https://wired.wubook.net/xrws/"


# TODO: faire attention au fichier de logs

# LOAD ID
user = os.getenv('WUBOOK_USER')
pkey = os.getenv('WUBOOK_PKEY')
lcode = os.getenv('WUBOOK_LCODE')
password = os.getenv('WUBOOK_PASSWORD')

MIN_PRICE = 45

class Wubook:
    def __init__(self, *args, **kwargs):
        self.server = xmlrpc.client.ServerProxy(url, verbose=False)
        self.returnCode, self.token = self.server.acquire_token(user, password, pkey)
        del password
        if self.returnCode != 0:
            logger.warning("Can’t connect to server")
        else:
            logger.info("Server connected")
    

    def __enter__(self):
        return self.server, self.token

    def __exit__(self, exception_type, exception_value, exception_traceback):
        if exception_type != None:
            logger.error(exception_value)
            logger.error(exception_traceback)
        else:
            if self.returnCode != 0:  # N’a pas pu se connecter au serveur
                pass
            else:
                try:
                    self.server.release_token(self.token)
                except xmlrpc.client.ProtocolError as e:
                    logger.warning(
                        f"ProtocolError while realeasing token from wubook server: \n{e}"
                    )
                else:
                    logger.info("Server disconnected")
    
    def fetch_rooms(self):
        return self.server.fetch_rooms(self.token, lcode)
    
    def fetch_room(self, room_id):
        return self.server.fetch_single_room(self.token, lcode, room_id)
    
    def room_images(self, room_id):
        return self.server.room_images(self.token, lcode, room_id)
    
    def fetch_rooms_values(self, dfrom, dto_included, rooms=None):
        """_summary_

        Args:
            dfrom (_type_): _description_
            dto_included (str): "DD/MM/YYYY la date est inclu. Donc pour un seul jour, dfrom = dto_included
            rooms (_type_, optional): _description_. Defaults to None.

        Returns:
            _type_: _description_
        """
        # return self.server.fetch_rooms_values(self.token, lcode, dfrom, dto, rooms)
        return self.server.fetch_rooms_values(self.token, lcode, dfrom, dto_included)

    def update_plan_prices(self, plan_id, dfrom, prices):
        """Upload prices
        
        Args:
        dfrom (str):  Starting Date (european format: 21/12/2021)
        prices (dict): Keys are Room IDs. Values are array of prices. Prices must be greater than 0.01. 
        plan_id (int): id of the plan prices.  0: Wubook Parity
        
        prices = {
            "1": [100, 101, 102],
            "2": [200, 201, 202]
        }
        
        Returns:
            _type_: _description_
        """
        return self.server.update_plan_prices(self.token, lcode, plan_id, dfrom, prices)
    
    def fetch_plan_prices(self, plan_id, dfrom, dto, rooms=None):
        """

        Args:
            token (_type_): _description_
            lcode (_type_): _description_
            pid (_type_): _description_
            dfrom (_type_): _description_
        """
        return self.server.fetch_plan_prices(self.token, lcode, plan_id, dfrom, dto, rooms)

    def fetch_booking(self, reservation_code, ancillary=False):
        return self.server.fetch_booking(self.token, lcode, reservation_code, ancillary)
    
    def new_reservation(self, dfrom, dto, rooms, customer, amount, origin, ccard, ancillary, guests, ignore_restrs, ignore_avail, status):
        """_summary_

        Args:
            dfrom = "14/12/2025"
            dto = "15/12/2025"
            customer = {
                "lname": "Nguyen6", 
                "fname": "Paul6",
                "email": "xuan.ng@hotmail.com",
                "city": "Grasse",
                "phone": "06 51 21 64 91",
                "street": "2 Cours Honoré Cresp",
                "country": "FR",
                "arrival_hour": "14h",
                "notes": "Test"
            }
            amount = 300
            rooms = {
                "329039" : [1, "nb"]
            }

            origin = "python"
            ccard = 0
            ancillary={
                'Room (1) NOM_DE_LA_CHAMBRE': {
                'Info': { 
                    "Prix": "<prix de la chambre>",
                    "Composition": "<Compositions>"
                    }
                },
                "Room (0) RESUMÉ": {
                    "Info": {
                        "Status": "Confirmé/Option",
                        "Date": "xx/xx/xxxx",
                        "Paiement": "En attente, Réglé, Sur place",
                        "Montant Paiement": "23"
                    }
                }
            }
            guests= {
                "men": 2,
                "children": 1
            }
            ignore_restrs=0
            ignore_avail=0
            status=1


        Returns:
            _type_: _description_
        """
        return self.server.new_reservation(self.token, lcode, dfrom, dto, rooms, customer, amount, origin, ccard, ancillary, guests, ignore_restrs, ignore_avail, status)
    

def get_avail(dfrom, dto):
    """Get avail from dfrom to dto in the wubook server
    dfrom and dto: dd/mm/yyyy
    if dfrom < dtoday: dfrom = dtoday
    RETURN {<date>: {
                     <code_chambre>:<disponibilité>, ...}
            ...}"""

    if datetime.strptime(dfrom, "%d/%m/%Y").date() < date.today():
        dfrom = date.today().strftime("%d/%m/%Y")
    with Wubook() as (server, token):
        return_code, avail = server.fetch_rooms_values(token, lcode, dfrom, dto)

    if return_code != 0:
        raise ConnectionError(f"in get_avail({dfrom}, {dto}) error: {avail}")

    dfrom_time = datetime.strptime(dfrom, "%d/%m/%Y")
    dto_time = datetime.strptime(dto, "%d/%m/%Y")
    days_diff = (dto_time - dfrom_time).days

    result = dict()
    for i in range(days_diff):
        temp_dict = dict()
        for room in type_room:
            temp_dict[room] = avail[room][i].get("avail", 0)
        result[(dfrom_time + timedelta(days=i)).strftime("%d/%m/%Y")] = temp_dict
    return result


def get_prices_avail_today():
    """Return prices and avails of today
    If time < 5:00 am: get the price of the previous day"""
    dnow = datetime.now()
    if 0 <= dnow.hour <= 5:
        dnow = datetime.now() - timedelta(days=1)
    dfrom = dto = dnow.strftime("%d/%m/%Y")
    with Wubook() as (server, token):
        return_code, plan_prices = server.fetch_plan_prices(token, lcode, 0, dfrom, dto)
        return_code2, avails = server.fetch_rooms_values(token, lcode, dfrom, dto)
    if return_code != 0:
        raise ConnectionError(f"in get_prices_today(), error: {plan_prices}")
    if return_code2 != 0:
        raise ConnectionError(f"in get_prices_today(), error: {avails}")
    return plan_prices, avails


def upload_prices(dfrom, prices, pid=0):
    """Upload prices

    Args:
        dfrom (str):  Starting Date (european format: 21/12/2021)
        prices (dict): Keys are Room IDs. Values are array of prices. Prices must be greater than 0.01. 
        pid (int, optional): id of the plan prices. Defaults to 0 (Wubook Parity) 
        
        prices = {
            "1": [100, 101, 102],
            "2": [200, 201, 202]
        }
    """
    with Wubook() as (server, token):
        logger.debug(server.update_plan_prices(token, lcode, pid, dfrom, prices))

