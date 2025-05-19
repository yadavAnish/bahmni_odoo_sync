from odoo import models, fields, api
import requests
import xml.etree.ElementTree as ET
from requests.auth import HTTPBasicAuth
import logging

_logger = logging.getLogger(__name__)


class BahmniSyncLog(models.Model):
    _name = "bahmni.sync.log"
    _description = "Synced Encounters"

    encounter_uuid = fields.Char(required=True, unique=True)
    patient_id = fields.Char()
    fee = fields.Float()
    synced_at = fields.Datetime(default=fields.Datetime.now)
    status = fields.Selection([
        ('success', 'Success'),
        ('failed', 'Failed')
    ])
    message = fields.Text()


class BahmniSyncEngine(models.Model):
    _name = 'bahmni.sync.engine'
    _description = "Bahmni Sync Engine"

    def sync_registration_fee(self):
        _logger.info("[START] Bahmni Registration Fee Sync Started")

        OPENMRS_URL = "http://bahmni-standard-openmrs-1:8080"
        ATOMFEED_URL = f"{OPENMRS_URL}/openmrs/ws/atomfeed/encounter/recent"
        _logger.info("[CONFIG] ATOMFEED_URL = %s", ATOMFEED_URL)

        AUTH = HTTPBasicAuth("admin", "Admin123")
        PRODUCT_NAME = "Registration Fee"
        SHOP_ID = 4
        PRICELIST_ID = 1

        try:
            response = requests.get(ATOMFEED_URL, headers={"Accept": "application/atom+xml"}, auth=AUTH)
            _logger.info("[HTTP] AtomFeed Response Status: %s", response.status_code)
            response.raise_for_status()
            _logger.info(f"[HTTP] AtomFeed Content: {response.text[:5000]}...")

            root = ET.fromstring(response.text)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            entries = root.findall('atom:entry', ns)
            _logger.info("[PARSE] Found %d entries in AtomFeed", len(entries))
        except Exception as e:
            _logger.error("[ERROR] Failed to fetch or parse AtomFeed", exc_info=True)
            return

        root = ET.fromstring(response.text)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}

        for entry in root.findall('atom:entry', ns):
            content_elem = entry.find('atom:content', ns)
            if content_elem is None or not content_elem.text:
                continue

            # content_url = content_elem.text.strip()
            content_url = f"{OPENMRS_URL}{content_elem.text.strip()}"

            encounter_uuid = content_url.split("/")[-1].split("?")[0]
            _logger.info("[INFO] Processing encounter: %s", content_url)

            if self.env['bahmni.sync.log'].search_count([('encounter_uuid', '=', encounter_uuid)]):
                _logger.info("[SKIP] Encounter %s already synced", encounter_uuid)
                continue

            try:
                encounter_response = requests.get(content_url, auth=AUTH)
                encounter_response.raise_for_status()
                data = encounter_response.json()
                _logger.debug("[DEBUG] Encounter Data: %s", data)

                patient_id = data.get("patientId")
                observations = data.get("observations", [])
                reg_fee_obs = next((o for o in observations if o.get("conceptNameToDisplay") == PRODUCT_NAME), None)

                if not reg_fee_obs:
                    _logger.info("[SKIP] No '%s' in encounter %s", PRODUCT_NAME, encounter_uuid)
                    continue

                fee_value = reg_fee_obs.get("value")
                _logger.info("[FOUND] Registration Fee = %s for patient %s", fee_value, patient_id)

                # Ensure Partner
                partner = self.env['res.partner'].search([('name', '=', patient_id)], limit=1)
                if not partner:
                    partner = self.env['res.partner'].create({'name': patient_id, 'customer_rank': 1})
                    _logger.info("[CREATE] Partner created: %s", partner.name)
                else:
                    _logger.info("[EXIST] Using existing partner: %s", partner.name)

                # Ensure Product
                product = self.env['product.product'].search([('name', '=', PRODUCT_NAME)], limit=1)
                if not product:
                    raise Exception(f"Product '{PRODUCT_NAME}' not found")

                # Create Sale Order
                order = self.env['sale.order'].create({
                    'partner_id': partner.id,
                    'pricelist_id': PRICELIST_ID,
                    'shop_id': SHOP_ID,
                    'order_line': [(0, 0, {
                        'product_id': product.id,
                        'product_uom_qty': 1,
                        'price_unit': fee_value,
                    })]
                })
                _logger.info("[SUCCESS] Created sale.order %s for patient %s", order.name, patient_id)

                self.env['bahmni.sync.log'].create({
                    'encounter_uuid': encounter_uuid,
                    'patient_id': patient_id,
                    'fee': fee_value,
                    'status': 'success',
                    'message': f"Order {order.name} created"
                })

            except Exception as e:
                _logger.error("[FAILURE] Encounter %s: %s", encounter_uuid, str(e), exc_info=True)
                self.env['bahmni.sync.log'].create({
                    'encounter_uuid': encounter_uuid,
                    'status': 'failed',
                    'message': str(e)
                })

        _logger.info("[DONE] Bahmni Registration Fee Sync Completed")
