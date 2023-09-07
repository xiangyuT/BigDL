import quote_generator
from flask import Flask, request
from configparser import ConfigParser
import ssl, os
import base64
import requests
import subprocess

app = Flask(__name__)
use_secure_cert = False

@app.route('/gen_quote', methods=['POST'])
def gen_quote():
    data = request.get_json()
    user_report_data = data.get('user_report_data')
    try:
        quote_b = quote_generator.generate_tdx_quote(user_report_data)
        quote = base64.b64encode(quote_b).decode('utf-8')
        return {'quote': quote}
    except Exception as e:
        return {'quote': "quote generation failed: %s" % (e)}

@app.route('/attest', methods=['POST'])
def get_cluster_quote_list():
    data = request.get_json()
    user_report_data = data.get('user_report_data')
    quote_list = []

    try:
        quote_b = quote_generator.generate_tdx_quote(user_report_data)
        quote = base64.b64encode(quote_b).decode("utf-8")
        quote_list.append(("launcher", quote))
    except Exception as e:
        quote_list.append("launcher", "quote generation failed: %s" % (e))

    command = "sudo -u mpiuser -E bash /ppml/get_worker_quote.sh"
    output = subprocess.check_output(command, shell=True)

    with open("/ppml/output/quote.log", "r") as quote_file:
        for line in quote_file:
            line = line.strip()
            if line:
                parts = line.split(":") 
                if len(parts) == 2:
                    quote_list.append((parts[0].strip(), parts[1].strip())) 
    return {"quote_list": dict(quote_list)}

if __name__ == '__main__':
    print("BigDL-AA: Agent Started.")
    app.run(host='0.0.0.0', port=9870)
