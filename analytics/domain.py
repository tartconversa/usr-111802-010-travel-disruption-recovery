"""旅客联程车票与最短站内换乘时间。"""
class JourneyDirectory:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS legs(journey TEXT, leg_no INTEGER, train TEXT, station TEXT, PRIMARY KEY(journey,leg_no))")
        connection.execute("CREATE TABLE IF NOT EXISTS interchange(station TEXT PRIMARY KEY, minutes INTEGER)")

    def add_leg(self, journey, leg_no, train, station):
        if type(leg_no) is not int or leg_no < 1: raise ValueError("行程序号无效")
        self.connection.execute("INSERT INTO legs VALUES(?,?,?,?)", (journey,leg_no,train,station))

    def set_interchange(self, station, minutes):
        if type(minutes) is not int or minutes < 0: raise ValueError("换乘时间无效")
        self.connection.execute("INSERT OR REPLACE INTO interchange VALUES(?,?)", (station,minutes))

    def legs(self, journey):
        return [tuple(row) for row in self.connection.execute("SELECT leg_no,train,station FROM legs WHERE journey=? ORDER BY leg_no", (journey,))]
