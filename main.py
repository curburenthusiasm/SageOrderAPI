from cryptography.fernet import Fernet
import uuid
import os
import pyodbc
import keyring
import requests
import json
from decimal import Decimal
import logging
import keyrings.alt


# Setup logging
log_path = os.path.join(os.getcwd(), 'app.log')  # define the absolute path for log file
logging.basicConfig(filename=log_path, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


with open('key.key', 'rb') as key_file:
    key = key_file.read()

# Create a cipher_suite with the key
cipher_suite = Fernet(key)

# Assuming you have the encrypted password saved in a .enc file
with open('password.enc', 'rb') as encrypted_password_file:
    encrypted_password = encrypted_password_file.read()

# Decrypt the password
password = cipher_suite.decrypt(encrypted_password).decode()


# Establish a connection to the database
conn = pyodbc.connect(
    'DRIVER={ODBC Driver 17 for SQL Server};'
    'SERVER=JEF-SQL;'
    'DATABASE=MAS_JEF;'
    'UID=MAS_REPORTS;'
    'PWD=' + password + ';'
)
logging.info('Connected to database')
# Create a cursor from the connection
cursor = conn.cursor()

# Append records from SONUOPENORDERS to SONUOPENORDERHISTORY
cursor.execute("""
    INSERT INTO MAS_JEF.dbo.SONUOPENORDERHISTORY
    SELECT * FROM MAS_JEF.dbo.SONUOPENORDERS where WarehouseCode is null
""")
conn.commit()

# Execute a SQL query to fetch the data
cursor.execute("SELECT * FROM MAS_JEF.dbo.SONUOPENORDERS where WarehouseCode is null")

# Get column names from cursor description
columns = [column[0] for column in cursor.description]

# Fetch the rows
rows = cursor.fetchall()

# Convert rows to list of dictionaries
data = []
for row in rows:
    row_dict = dict(zip(columns, row))
    data.append(row_dict)

# Obtain the access/bearer token
token_url = "https://login.microsoftonline.com/974b2ee7-8fca-4ab4-948a-02becfbf058f/oauth2/v2.0/token"
token_data = {
    "client_id": "4813f133-8480-4975-ac1a-23da10fdb697",
    "client_secret": "SeW8Q~TvLvuDJQV32JAUUFOD.SUG9DB86Gqpscx2",
    "scope": "api://17d2b604-8930-40bc-81ab-583fdca33c6f/.default",
    "grant_type": "client_credentials"
}
response = requests.post(token_url, data=token_data, headers={'Content-Type': 'application/x-www-form-urlencoded'})

logging.info(f'Response from token request: {response.text}')
response.raise_for_status()  # raise exception if invalid response
token = response.json().get('access_token')

# Step 2: Use the token to make a post call to SalesOrderHeaders endpoint
sales_order_url = "https://roiconsultingapi.azurewebsites.net/api/v2/sales_order_headers"
headers = {
    "Authorization": "Bearer " + token,
    "Content-Type": "application/json"
}
# Assume you have this mapping from the document:
field_mapping = {
    "SalesOrderNo": "SalesOrderNo",
    "ItemCode": "ItemCode",
    "QuantityOrdered": "QuantityOrdered",
    "CustomerNo": "CustomerNo",
    "ShipToName": "ShipToName",
    "ShipToAddress1": "ShipToAddress1",
    "ShipToAddress2": "ShipToAddress2",
    "ShipToCity": "ShipToCity",
    "ShipToState": "ShipToState",
    "ShipToZipCode": "ShipToZipCode",
    "ShipToCountryCode": "ShipToCountryCode",
    "CustomerPONo": "CustomerPONo",
    "UDF_SHIP_TO_PHONE": "UDF_SHIP_TO_PHONE",
    "OrderType": "OrderType",
    "BillToName": "BillToName",
    "BillToAddress1": "BillToAddress1",
    "BillToAddress2": "BillToAddress2",
    "BillToAddress3": "BillToAddress3",
    "BillToCity": "BillToCity",
    "BillToState": "BillToState",
    "BillToZipCode": "BillToZipCode",
    "BillToCountryCode": "BillToCountryCode",


    # Add all other fields here
}

# Loop through the data and make a POST request for each item
for item in data:
    # Build the request data from the SQL data
    request_data = {field: item[column] for field, column in field_mapping.items()}
    request_data['SalesOrderDetails'] = [{
        "SalesOrderNo": item["SalesOrderNo"],
        "LineKey": item["LineKey"],
        "ItemCode": item["ItemCode"],
        "QuantityOrdered": item["QuantityOrdered"],
    }]
    request_data['OrderType'] = 'S'
    request_data['BillToName'] = 'SONU Sleep Corporation'
    request_data['BillToAddress1'] = '8033 W Sunset Blvd,'
    request_data['BillToAddress2'] = '#PMB 963'
    request_data['BillToAddress3'] = '  '
    request_data['BillToCity'] = 'Los Angeles'
    request_data['BillToState'] = 'CA'
    request_data['BillToZipCode'] = '90046'
    request_data['BillToCountryCode'] = 'USA'

    class DecimalEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, Decimal):
                return str(obj)
            return super(DecimalEncoder, self).default(obj)

    response = requests.post(sales_order_url, headers=headers, data=json.dumps(request_data, cls=DecimalEncoder))
    logging.info(f'Response from sales order request: {response.text}')
    response.raise_for_status()  # raise exception if invalid response
    logging.info(f'JSON response: {response.json()}')

# Delete rows from the SO Header where BillToName is blank and SO detail where itemtype is null
try:
    cursor.execute("""
    DELETE FROM MAS_JEF.dbo.SO_SalesOrderHeader WHERE BillToName is null
DELETE FROM MAS_JEF.dbo.SO_SalesOrderDetail WHERE ItemType is null 
    """)

    conn.commit()
    logging.info('deleted order')
except Exception as e:
    logging.error(f"Error occurred while deleting rows: {str(e)}")

# Close the cursor and connection
cursor.close()
conn.close()

logging.info('Cursor and connection closed')
