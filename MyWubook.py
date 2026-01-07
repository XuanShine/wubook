
import os
import datetime as dt

from .wubook_api import Wubook

from loguru import logger
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
YAML_ROOMS = os.path.join(BASE_DIR, 'room_config.yml')

with open(YAML_ROOMS, "r") as f_in:
    HIDE_ROOMS = yaml.safe_load(f_in)["hide_rooms"]

class MyWubook(Wubook):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ROOMS = self.fetch_rooms()[1]
        self.ID_TO_ROOMS = {x["id"]: x["name"] for x in self.ROOMS}
        self.SUBROOMS = {x["id"]: x["subroom"] for x in self.ROOMS if x["subroom"]}
        self.OCCUPANCY = {x["id"]: x["occupancy"] for x in self.ROOMS}

    def __enter__(self):
        return self
    

    def get_rooms_between(self, date_in:dt.date, date_out:dt.date):
        """
        (i): on retire les chambres dans "HIDE_ROOMS"
        
        date: datetime

        RETURN
        {'329039': 
            {'name': 'Double Classique',
            "occupancy": 1,
            'price': 245.51500000000001,
            'avail': 6,
            'closed': 0,
            'min_stay': 0},
        '329667':
            {'name': 'Double Balcon',
            'price': 282.34225000000004,
            "occupancy": 2,
            'avail': 2,
            'closed': 0,
            'min_stay': 0}, ...}
        """
        date_in = date_in.strftime("%d/%m/%Y")
        date_out = (date_out - dt.timedelta(days=1)).strftime("%d/%m/%Y")
        
        res = self.fetch_rooms_values(date_in, date_out)
        if res[0] != 0:
            error_message = f"In MyWubook.get_rooms_between {date_in} {date_out}\n ERROR message server: {res}"
            logger.error(error_message)
            raise ConnectionError(error_message)
        res = res[1]

        # Formattage des résultats
        resultat = dict()
        for key, value in res.items():
            if int(key) in HIDE_ROOMS:
                continue
            tmp = {"name": self.ID_TO_ROOMS[int(key)],
                   "occupancy" : self.OCCUPANCY[int(key)]}
            for date in value:
                tmp["price"] = tmp.get("price", 0) + date["price"]
                tmp["avail"] = min(tmp.get("avail", 50), date.get("avail", 0))
                tmp["closed"] = max(tmp.get("closed", 0), date["closed"])
                tmp["min_stay"] = max(tmp.get("min_stay", 0), date["min_stay"])
            tmp["price"] = round(tmp["price"], 2)
            resultat[key] = tmp

        # Réajustement des disponibilités des sous-chambres
        for key, data in resultat.items():
            if int(key) in self.SUBROOMS:
                data["avail"] = resultat[str(self.SUBROOMS[int(key)])]["avail"]
       
        return resultat
