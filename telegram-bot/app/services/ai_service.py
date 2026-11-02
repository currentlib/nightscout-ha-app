import base64
import logging
import aiohttp
import urllib.parse
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Any, AsyncGenerator
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from app.config import settings
from app.services.usage import record_usage
from app.services.tiers import resolve_heavy_model, resolve_light_model

logger = logging.getLogger(__name__)

class ImageDetails(BaseModel):
    barcode: Optional[str] = Field(None, description="Barcode digits if a barcode is clearly visible in the image")
    product_name: Optional[str] = Field(None, description="Precise name of the product including brand, flavor, package size/weight if it's a packaged brand product")
    is_packaged: bool = Field(..., description="True if this is a packaged branded retail product, False if freshly prepared meal or raw food")
    dish_name: Optional[str] = Field(
        None,
        description=(
            "If is_packaged=False (a home-cooked/prepared meal or raw food), a short name for the dish "
            "in Ukrainian, e.g. 'Борщ', 'Смажена картопля з котлетою', 'Грецький салат'. "
            "Leave null if is_packaged=True or the dish cannot be identified."
        )
    )


class FoodNutritionSchema(BaseModel):
    """The schema OpenAI sees — NO internal fields like data_source."""
    needs_clarification: bool = Field(
        default=False,
        description=(
            "Set to True ONLY if you cannot produce a reasonable nutrition estimate without more information. "
            "Examples: serving size is completely unknown (e.g. a bowl with no visual cues), "
            "the image is blurry or ambiguous. Do NOT ask if you can make a reasonable estimate."
        )
    )
    clarification_question: Optional[str] = Field(
        default=None,
        description=(
            "If needs_clarification=True, write ONE short question in Ukrainian to ask the user. "
            "Ask only what is strictly needed to estimate the portion: e.g. "
            "'Яка приблизна вага порції (в грамах)?' or 'Скільки шматочків ви з\'їли?'. "
            "If needs_clarification=False, leave this null."
        )
    )
    portion_estimate: str = Field(
        ...,
        description=(
            "State this FIRST, before computing any macros below. Identify the food item(s) and estimate "
            "their portion weight in grams, reasoning from visual/contextual size cues (e.g. plate diameter "
            "~26cm, cup ~250ml, fist ~150g) or from the quantities described in the text. "
            "Example: 'Борщ, одна тарілка, приблизно 350г' or 'Середнє яблуко, ~150г'. "
            "Base all macro values below on this stated weight."
        )
    )
    carbohydrates: float = Field(..., description="Carbohydrates in grams for the described serving/portion")
    protein: float = Field(..., description="Protein in grams for the described serving/portion")
    fat: float = Field(..., description="Fat in grams for the described serving/portion")
    calories: float = Field(..., description="Calories in kcal for the described serving/portion")
    sugar: float = Field(..., description="Sugar in grams for the described serving/portion")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Confidence score 0.0-1.0: use >0.9 for verified database data, 0.7-0.9 for web search, <0.7 for visual estimate")
    ingredients: list[str] = Field(..., description="List of ingredients or food components, translated to Ukrainian")


class FoodAnalysis(BaseModel):
    """Internal model — includes data_source set by our code, not by OpenAI."""
    portion_estimate: str = ""
    carbohydrates: float
    protein: float
    fat: float
    calories: float
    sugar: float
    confidence_score: float
    ingredients: list[str]
    data_source: str = "ai"
    needs_clarification: bool = False
    clarification_question: Optional[str] = None

    @classmethod
    def from_schema(cls, schema: FoodNutritionSchema, data_source: str = "ai") -> "FoodAnalysis":
        return cls(
            portion_estimate=schema.portion_estimate,
            carbohydrates=schema.carbohydrates,
            protein=schema.protein,
            fat=schema.fat,
            calories=schema.calories,
            sugar=schema.sugar,
            confidence_score=schema.confidence_score,
            ingredients=schema.ingredients,
            data_source=data_source,
            needs_clarification=schema.needs_clarification,
            clarification_question=schema.clarification_question,
        )



class ExtractedMetrics(BaseModel):
    glucose_level: Optional[float] = Field(None, description="Blood glucose level in mmol/L if mentioned (e.g. 'цукор 6.5', 'глюкоза 5.8', 'цукор зараз 8.0').")
    insulin_dosage: Optional[float] = Field(None, description="Insulin dosage in units if mentioned as injected/given (e.g. 'вколов 4 ОД', 'ввів 3 інсуліну').")
    mentioned_food: Optional[str] = Field(
        None,
        description=(
            "Fill this if the user says they are currently eating, about to eat, or just ate a specific "
            "food/dish/meal RIGHT NOW — not a past day, not hypothetical, not a general question about "
            "nutrition/diet. Write the food description in Ukrainian, including any quantity/portion the "
            "user mentioned (e.g. 'бутерброд з сиром', 'яблуко, 20г вуглеводів', 'обід: курка з рисом'). "
            "Do NOT fill this for vague statements with no identifiable food ('зголоднів', 'хочу їсти' with "
            "nothing named), for questions or advice-seeking about food/diet, or for food already logged "
            "earlier. Leave null otherwise."
        )
    )
    memory_fact: Optional[str] = Field(
        None,
        description=(
            "A NEW durable, long-term fact about the user worth remembering across future conversations, "
            "stated in Ukrainian in a short sentence — e.g. an allergy or intolerance, a food dislike/preference, "
            "a doctor's recommendation, a medication, or a routine/habit (e.g. 'Алергія на горіхи', "
            "'Не любить гострої їжі', 'Лікар порадив менше вуглеводів ввечері', 'Зазвичай робить ін'єкції в живіт'). "
            "Only extract facts that are stable/lasting, not one-off events (do NOT extract 'з'їв яблуко' or 'цукор 6.5' here — "
            "those are logged separately). Return null if nothing new and durable is stated."
        )
    )


async def web_search_duckduckgo(query: str) -> str:
    """Performs a web search via DuckDuckGo HTML search and returns snippets."""
    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    snippets = []
                    for result in soup.find_all("a", class_="result__snippet"):
                        snippets.append(result.get_text(strip=True))
                    if snippets:
                        return "\n".join(snippets[:5])
    except Exception as e:
        logger.error(f"Error scraping DuckDuckGo: {e}")
    return ""


async def fetch_product_by_barcode(barcode: str) -> Optional[Dict[str, Any]]:
    """Queries Open Food Facts API for a product by barcode."""
    url = f"https://world.openfoodfacts.org/api/v2/product/{barcode.strip()}.json"
    headers = {"User-Agent": "DiabetesHelperBot/1.0 (support@diabetesbot.com)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    if res_json.get("status") == 1 or res_json.get("status_verbose") == "product found":
                        return res_json.get("product")
    except Exception as e:
        logger.error(f"Error fetching product from Open Food Facts: {e}")
    return None


async def search_product_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Searches Open Food Facts by product name."""
    encoded_name = urllib.parse.quote_plus(name)
    url = f"https://world.openfoodfacts.org/cgi/search.pl?search_terms={encoded_name}&search_simple=1&action=process&json=1"
    headers = {"User-Agent": "DiabetesHelperBot/1.0 (support@diabetesbot.com)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    products = res_json.get("products", [])
                    if products:
                        for prod in products[:3]:
                            nutriments = prod.get("nutriments", {})
                            if nutriments.get("carbohydrates_100g") is not None:
                                return prod
    except Exception as e:
        logger.error(f"Error searching product by name on Open Food Facts: {e}")
    return None


async def fetch_usda_nutrition(query: str) -> Optional[Dict[str, Any]]:
    """Searches USDA FoodData Central and returns a dict shaped like an Open Food Facts
    product (product_name/quantity/ingredients_text/nutriments) so it plugs directly into
    analyze_with_product_context without any changes there."""
    url = "https://api.nal.usda.gov/fdc/v1/foods/search"
    params = {"query": query, "api_key": settings.USDA_API_KEY, "pageSize": 3}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    for food in res_json.get("foods", []):
                        nutrients = {n.get("nutrientName"): n for n in food.get("foodNutrients", [])}
                        carbs = nutrients.get("Carbohydrate, by difference", {}).get("value")
                        if carbs is None:
                            continue
                        energy = next(
                            (n.get("value") for n in food.get("foodNutrients", [])
                             if n.get("nutrientName") == "Energy" and n.get("unitName") == "KCAL"),
                            None
                        )
                        sugar = nutrients.get("Sugars, total including NLEA", nutrients.get("Total Sugars", {})).get("value")
                        return {
                            "product_name": food.get("description"),
                            "quantity": "",
                            "ingredients_text": "",
                            "nutriments": {
                                "carbohydrates_100g": carbs,
                                "proteins_100g": nutrients.get("Protein", {}).get("value"),
                                "fat_100g": nutrients.get("Total lipid (fat)", {}).get("value"),
                                "energy-kcal_100g": energy,
                                "sugars_100g": sugar,
                            }
                        }
    except Exception as e:
        logger.error(f"Error fetching nutrition from USDA FoodData Central: {e}")
    return None


class AIService:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def extract_metrics_from_text(self, text: str) -> Optional[ExtractedMetrics]:
        """
        Extracts blood glucose, insulin dosage, carbs, and any new durable personal fact
        (allergy, preference, routine) from general user text messages.
        """
        extraction_instructions = (
            "Extract blood glucose level or insulin dosage from the user's message if explicitly mentioned. "
            "Do not invent metrics; only extract if they are explicitly stated in the message. "
            "Also check if the user is currently eating, about to eat, or just ate a specific food — see the "
            "mentioned_food field description for exact rules on when to fill it. "
            "Also check if the message reveals a NEW durable long-term fact about the user (allergy, food "
            "preference/dislike, doctor's recommendation, medication, or routine/habit) and fill memory_fact if so. "
            "If nothing is mentioned, return all fields as null."
        )
        try:
            response = await self.client.beta.chat.completions.parse(
                model=resolve_light_model(),
                messages=[
                    {
                        "role": "system",
                        "content": extraction_instructions
                    },
                    {
                        "role": "user",
                        "content": text
                    }
                ],
                response_format=ExtractedMetrics
            )
            await record_usage(resolve_light_model(), response.usage.prompt_tokens, response.usage.completion_tokens, "extraction")
            return response.choices[0].message.parsed
        except Exception as e:
            logger.error(f"Error extracting metrics: {e}")
            return None

    async def extract_image_details(self, photo_bytes: bytes) -> Optional[ImageDetails]:
        """Uses GPT vision to inspect the image and extract barcode/product details, or a dish name for home-cooked food."""
        image_instructions = (
            "Analyze the image and check if it contains a packaged retail food product. "
            "If a barcode is visible in the image, extract the digits exactly. "
            "Try to identify the precise product name, brand, flavor, and package size (e.g. 'Coca Cola 0.5L', 'Snickers 50g'). "
            "If it is NOT a packaged product (a home-cooked/prepared meal or raw food instead), "
            "identify and name the dish in Ukrainian instead (fill dish_name, leave barcode/product_name null). "
            "Return your findings as structured JSON matching ImageDetails."
        )
        try:
            base64_image = base64.b64encode(photo_bytes).decode("utf-8")
            messages = [
                {
                    "role": "system",
                    "content": image_instructions
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ]
            response = await self.client.beta.chat.completions.parse(
                model=resolve_heavy_model(),
                messages=messages,
                response_format=ImageDetails,
                max_completion_tokens=1000
            )
            await record_usage(resolve_heavy_model(), response.usage.prompt_tokens, response.usage.completion_tokens, "food_analysis")
            return response.choices[0].message.parsed
        except Exception as e:
            logger.error(f"Error extracting image details: {e}")
            return None

    async def analyze_with_product_context(self, product_data: dict, photo_bytes: Optional[bytes] = None, text_description: Optional[str] = None) -> FoodAnalysis:
        """Sends Open Food Facts product details to GPT to estimate serving size and output structured FoodAnalysis."""
        nutriments = product_data.get("nutriments", {})
        carbs_100g = nutriments.get("carbohydrates_100g")
        protein_100g = nutriments.get("proteins_100g")
        fat_100g = nutriments.get("fat_100g")
        calories_100g = nutriments.get("energy-kcal_100g")
        sugar_100g = nutriments.get("sugars_100g")
        product_name = product_data.get("product_name") or product_data.get("product_name_en") or "Продукт"
        package_quantity = product_data.get("quantity") or ""
        ingredients_text = product_data.get("ingredients_text") or ""

        context_prompt = (
            f"Verified Nutrition Facts per 100g for product '{product_name}' (Weight: {package_quantity}):\n"
            f"- Carbohydrates: {carbs_100g}g\n"
            f"- Protein: {protein_100g}g\n"
            f"- Fat: {fat_100g}g\n"
            f"- Calories: {calories_100g} kcal\n"
            f"- Sugar: {sugar_100g}g\n"
            f"Original ingredients list: {ingredients_text}\n\n"
            "Based on this verified nutrition data and the user's uploaded image/description, "
            "estimate the serving weight of the food item in the image/description, "
            "scale the carbohydrates, protein, fat, calories, and sugar values to match the estimated serving size. "
            "Translate all ingredients to Ukrainian. "
            "If the user specifies they ate the whole package, or the package size matches the image, use the full package values."
        )

        system_prompt = (
            "You are an expert clinical dietitian and diabetes assistant. "
            "You will calculate final serving size nutrition facts using the verified per-100g details provided in user message context. "
            "Write ingredients list in Ukrainian. Set confidence_score to 0.95 or higher since data is verified. "
            "Set needs_clarification=False — the per-100g nutrition data is already verified, only the serving size needs estimating."
        )

        messages = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]

        user_content = [{"type": "text", "text": context_prompt}]
        if text_description:
            user_content.append({"type": "text", "text": f"User description: {text_description}"})
        if photo_bytes:
            base64_image = base64.b64encode(photo_bytes).decode("utf-8")
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
            })

        messages.append({"role": "user", "content": user_content})

        response = await self.client.beta.chat.completions.parse(
            model=resolve_heavy_model(),
            messages=messages,
            response_format=FoodNutritionSchema,
            max_completion_tokens=1000
        )
        await record_usage(resolve_heavy_model(), response.usage.prompt_tokens, response.usage.completion_tokens, "food_analysis")
        raw = response.choices[0].message.parsed
        if not raw:
            raise ValueError("Failed to parse product context analysis.")
        return FoodAnalysis.from_schema(raw, data_source="barcode_db")

    async def analyze_with_search_context(self, product_name: str, search_snippets: str, photo_bytes: Optional[bytes] = None, text_description: Optional[str] = None) -> FoodAnalysis:
        """Uses DuckDuckGo search snippets to extract and scale nutrition facts."""
        context_prompt = (
            f"We searched the web for nutrition facts for '{product_name}'. Here are the search results:\n"
            f"{search_snippets}\n\n"
            "Based on these search results and the user's uploaded image/description, "
            "identify the carbohydrate, protein, fat, calories, and sugar values per 100g or per serving. "
            "Then estimate the serving size of the food in the image/description and scale the values accordingly. "
            "Translate all ingredients to Ukrainian. Set confidence_score to 0.85 or higher if search data is relevant."
        )

        system_prompt = (
            "You are an expert clinical dietitian and diabetes assistant. "
            "Extract nutrition facts from the search snippets provided in user context and calculate serving values. "
            "Write ingredients list in Ukrainian. "
            "Set needs_clarification=False — use the search results as the best available data and make a reasonable estimate."
        )

        messages = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]

        user_content = [{"type": "text", "text": context_prompt}]
        if text_description:
            user_content.append({"type": "text", "text": f"User description: {text_description}"})
        if photo_bytes:
            base64_image = base64.b64encode(photo_bytes).decode("utf-8")
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
            })

        messages.append({"role": "user", "content": user_content})

        response = await self.client.beta.chat.completions.parse(
            model=resolve_heavy_model(),
            messages=messages,
            response_format=FoodNutritionSchema,
            max_completion_tokens=1000
        )
        await record_usage(resolve_heavy_model(), response.usage.prompt_tokens, response.usage.completion_tokens, "food_analysis")
        raw = response.choices[0].message.parsed
        if not raw:
            raise ValueError("Failed to parse search context analysis.")
        return FoodAnalysis.from_schema(raw, data_source="web_search")

    async def analyze_food(
        self,
        text_description: Optional[str] = None,
        photo_bytes: Optional[bytes] = None
    ) -> FoodAnalysis:
        """
        Analyzes a food description or photo using GPT with structured JSON outputs.
        Utilizes a fallback chain: Barcode -> Open Food Facts (by barcode/name) -> USDA FDC
        (packaged-product name only; skipped for home-cooked/described dishes since those are
        named in Ukrainian and USDA has no multilingual matching) -> DDG Search -> Visual.
        """
        details = None
        if photo_bytes:
            details = await self.extract_image_details(photo_bytes)
            if details and details.is_packaged:
                logger.info(f"Image details extracted: barcode={details.barcode}, product_name={details.product_name}")

                # 2. Try barcode lookup
                if details.barcode:
                    logger.info(f"Attempting barcode lookup for barcode: {details.barcode}")
                    product_data = await fetch_product_by_barcode(details.barcode)
                    if product_data:
                        logger.info(f"Product found in Open Food Facts by barcode: {product_data.get('product_name')}")
                        try:
                            result = await self.analyze_with_product_context(product_data, photo_bytes, text_description)
                            result.data_source = "barcode_db"
                            return result
                        except Exception as e:
                            logger.error(f"Error parsing with product context: {e}. Falling back...")

                # 3. Try name lookup in Open Food Facts
                if details.product_name:
                    logger.info(f"Attempting search by product name in Open Food Facts: {details.product_name}")
                    product_data = await search_product_by_name(details.product_name)
                    if product_data:
                        logger.info(f"Product found in Open Food Facts by name search: {product_data.get('product_name')}")
                        try:
                            result = await self.analyze_with_product_context(product_data, photo_bytes, text_description)
                            result.data_source = "name_db"
                            return result
                        except Exception as e:
                            logger.error(f"Error parsing with product context: {e}. Falling back...")

                    # 4. Try USDA FoodData Central (structured data, before falling to scraping)
                    logger.info(f"Attempting USDA lookup for: {details.product_name}")
                    usda_data = await fetch_usda_nutrition(details.product_name)
                    if usda_data:
                        try:
                            result = await self.analyze_with_product_context(usda_data, photo_bytes, text_description)
                            result.data_source = "usda"
                            return result
                        except Exception as e:
                            logger.error(f"Error parsing with USDA context: {e}. Falling back...")

                    # 5. Try Web Search via DuckDuckGo
                    logger.info(f"Attempting web search lookup for: {details.product_name}")
                    search_query = f"{details.product_name} nutrition facts БЖВ вуглеводи"
                    snippets = await web_search_duckduckgo(search_query)
                    if snippets:
                        logger.info(f"Search results found. Attempting analysis with search context...")
                        try:
                            result = await self.analyze_with_search_context(details.product_name, snippets, photo_bytes, text_description)
                            result.data_source = "web_search"
                            return result
                        except Exception as e:
                            logger.error(f"Error parsing with search context: {e}. Falling back...")

        # 4a. Photo of a home-cooked dish with no caption: extract_image_details already ran
        # above and, for non-packaged food, tried to name the dish for free — use that name to
        # search for real nutrition data instead of jumping straight to a blind vision guess.
        if photo_bytes and not text_description and details and not details.is_packaged and details.dish_name:
            logger.info(f"Attempting web search lookup for dish identified from photo: {details.dish_name}")
            search_query = f"{details.dish_name} калорійність БЖВ склад"
            snippets = await web_search_duckduckgo(search_query)
            if snippets:
                logger.info("Search results found for photo-identified dish. Attempting analysis with search context...")
                try:
                    result = await self.analyze_with_search_context(details.dish_name, snippets, photo_bytes, text_description)
                    result.data_source = "web_search"
                    return result
                except Exception as e:
                    logger.error(f"Error parsing with search context for photo-identified dish: {e}. Falling back...")

        # 4b. Home-cooked / described dishes have no barcode to ground on, but if the user
        # gave a text description (typed, or a photo caption) we can still search for real
        # nutrition data on the dish itself before resorting to a blind vision estimate.
        if text_description:
            logger.info(f"Attempting web search lookup for described dish: {text_description}")
            search_query = f"{text_description} калорійність БЖВ склад"
            snippets = await web_search_duckduckgo(search_query)
            if snippets:
                logger.info("Search results found for described dish. Attempting analysis with search context...")
                try:
                    result = await self.analyze_with_search_context(text_description, snippets, photo_bytes, text_description)
                    result.data_source = "web_search"
                    return result
                except Exception as e:
                    logger.error(f"Error parsing with search context for described dish: {e}. Falling back...")

        # 5. Default Visual / Text Estimate Fallback
        system_prompt = (
            "You are a clinical dietitian specialized in diabetes nutrition management. "
            "Your task is to accurately estimate the nutritional composition of the food shown. "
            "Be precise and realistic — do not round values to multiples of 5 or 10 unless they truly are round. "
            "Follow these rules:\n"
            "1. Estimate the PORTION SIZE visible in the image or described in the text (in grams). "
            "Typical home portions: a slice of bread ~30g, a cup of rice cooked ~180g, a medium apple ~150g, a glass of juice ~200ml.\n"
            "2. Calculate carbohydrates, protein, fat, sugar and calories BASED on that portion size, not per 100g.\n"
            "3. Use standard nutritional databases as reference (USDA, Atwater values).\n"
            "4. Set confidence_score high (0.80+) only if you are certain about what the food is and its portion. "
            "If uncertain about portion size but the food type is clear, make your BEST ESTIMATE and set confidence_score lower (0.5-0.7).\n"
            "5. Set needs_clarification=True ONLY if you truly cannot estimate even a ballpark figure — "
            "e.g. the image is completely unrecognizable, or the text is too vague (e.g. 'I ate something'). "
            "For typical meals like soups, salads, baked goods — always attempt an estimate first.\n"
            "6. Translate ALL ingredient names to Ukrainian.\n"
            "7. For mixed dishes (e.g. borscht, pizza, salad), break down each component."
        )

        messages = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]

        user_content = []
        if text_description:
            user_content.append({"type": "text", "text": text_description})

        if photo_bytes:
            base64_image = base64.b64encode(photo_bytes).decode("utf-8")
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_image}",
                    "detail": "high"
                }
            })

        if not user_content:
            raise ValueError("Either text_description or photo_bytes must be provided.")

        messages.append({"role": "user", "content": user_content})

        try:
            heavy_model = resolve_heavy_model()
            logger.info(f"Sending visual/text analysis to OpenAI model {heavy_model}...")
            response = await self.client.beta.chat.completions.parse(
                model=heavy_model,
                messages=messages,
                response_format=FoodNutritionSchema,
                max_completion_tokens=1000
            )
            await record_usage(resolve_heavy_model(), response.usage.prompt_tokens, response.usage.completion_tokens, "food_analysis")
            raw = response.choices[0].message.parsed
            if not raw:
                raise ValueError("Failed to parse structured output from OpenAI.")
            return FoodAnalysis.from_schema(raw, data_source="ai")
        except Exception as e:
            logger.error(f"Error calling OpenAI API: {e}")
            raise e

    async def analyze_food_clarified(
        self,
        user_answer: str,
        original_text: Optional[str] = None,
        photo_bytes: Optional[bytes] = None,
    ) -> FoodAnalysis:
        """
        Re-runs food analysis after the user has answered a clarification question.
        The original photo/text are included as context alongside the user's answer.
        """
        system_prompt = (
            "You are a clinical dietitian specialized in diabetes nutrition management. "
            "The user was previously asked a clarification question about their meal. "
            "They have now provided the additional information. "
            "Using ALL available context (the original image/description + their answer), "
            "produce an accurate nutritional estimate. "
            "Rules:\n"
            "1. Calculate values for the actual portion consumed, not per 100g.\n"
            "2. Use USDA/Atwater reference values.\n"
            "3. Be precise — do not round to multiples of 5 unless accurate.\n"
            "4. Translate ALL ingredients to Ukrainian.\n"
            "5. Set needs_clarification=False — you now have enough information to estimate."
        )

        user_content = []

        if original_text:
            user_content.append({"type": "text", "text": f"Original food description: {original_text}"})

        if photo_bytes:
            base64_image = base64.b64encode(photo_bytes).decode("utf-8")
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_image}",
                    "detail": "high"
                }
            })

        user_content.append({"type": "text", "text": f"User's clarification answer: {user_answer}"})

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        logger.info("Sending clarified food analysis to OpenAI...")
        response = await self.client.beta.chat.completions.parse(
            model=resolve_heavy_model(),
            messages=messages,
            response_format=FoodNutritionSchema,
            max_completion_tokens=1000
        )
        await record_usage(resolve_heavy_model(), response.usage.prompt_tokens, response.usage.completion_tokens, "food_analysis")
        raw = response.choices[0].message.parsed
        if not raw:
            raise ValueError("Failed to parse clarified food analysis.")
        return FoodAnalysis.from_schema(raw, data_source="ai")

    async def chat_with_assistant(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        user_profile: Dict[str, Any],
        recent_logs: List[Dict[str, Any]],
        recent_injections: List[Dict[str, Any]],
        lookback_hours: int = 24,
        pending_meal_context: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        Allows the user to converse with the AI chatbot, carrying history and profile context.
        Streams the reply: yields the accumulated text so far each time new tokens arrive,
        so the caller can show a live "typing" effect instead of waiting for the full reply.
        """
        # Format recent history compactly
        logs_list = []
        for log in recent_logs:
            parts = []
            if log.get('glucose') is not None:
                parts.append(f"Ц:{log.get('glucose')}")
            if log.get('carbs') is not None:
                meal_info = f"Їжа: {log.get('desc') or 'Страва'} (В:{log.get('carbs')}г"
                if log.get('protein'):
                    meal_info += f", Б:{log.get('protein')}г"
                if log.get('fat'):
                    meal_info += f", Ж:{log.get('fat')}г"
                if log.get('sugar'):
                    meal_info += f", Ц:{log.get('sugar')}г"
                if log.get('calories'):
                    meal_info += f", К:{log.get('calories')}ккал"
                meal_info += ")"
                parts.append(meal_info)
            if parts:
                logs_list.append(f"- {log.get('timestamp')}: {', '.join(parts)}")
        logs_summary = "\n".join(logs_list) if logs_list else "Нічого."

        injections_summary = "\n".join([
            f"- {inj.get('timestamp')}: {inj.get('dosage')}ОД ({'болюс' if inj.get('type') == 'bolus' else 'базал'})"
            for inj in recent_injections
        ]) if recent_injections else "Нічого."

        # NOTE on ordering: OpenAI prompt caching matches the longest common PREFIX across
        # requests. Everything that never changes (the rules block) is written first so it
        # stays byte-identical call to call (and even shared across different users);
        # everything below "=== USER CONTEXT ===" is ordered from least to most volatile
        # (profile settings rarely change, IOB/time/logs change on every single message) so
        # the cache hit extends as far as possible before hitting the first real diff.
        system_content = (
            "You are an expert personal diabetes coach. Help the user. Respond in Ukrainian.\n\n"
            "=== STRICT OPERATIONAL RULES ===\n"
            "1. DATA-FIRST LOOKUP: You MUST ALWAYS search the provided context (clinical settings, physical profile, 'Known long-term facts', 'Currently reviewing meal', 'Glucose & Meals', and 'Insulin Injections') before asking the user for ANY historical metrics or personal details. "
            "If the user asks 'when I injected/ate', 'how much insulin', or 'what was my last sugar', you must retrieve the answer from the history list and cite the exact timestamp. Never say 'I don't know' or ask the user 'When did you...' if it is listed in the history. "
            "Respect known long-term facts (allergies, dislikes, doctor's advice) in every suggestion you make, without the user having to repeat them. "
            "If a 'Currently reviewing meal' is provided, assume questions about splitting insulin, timing, or dosing refer to THAT meal unless the user clearly means something else — use its FPU analysis if present instead of recomputing it yourself.\n"
            "2. INSULIN STACKING WARNING & IOB SAFETY: You MUST examine the 'Current Calculated Active Insulin (IOB)' value. If the user asks whether to inject more insulin (correction dose) and IOB is greater than 0, you MUST explicitly warn them against 'insulin stacking' (overlapping insulin action leading to hypoglycemia). "
            "Remind them that active insulin is still working to lower their blood sugar, show the remaining IOB, and subtract it from any correction dose. Suggest waiting for the DIA (duration of insulin action) to finish unless they are correcting a verified high sugar that isn't budging.\n"
            "3. STEP-BY-STEP CLINICAL CALCULATIONS: When suggesting insulin corrections or food boluses, explain your calculations step-by-step: "
            "a) Meal Dose = Carbs / ICR\n"
            "b) Correction Dose = (Current Glucose - Target Glucose) / ISF\n"
            "c) Suggested Dose = Meal Dose + Correction Dose - IOB\n"
            "Show the numbers plugged into these formulas so the user can double check your math. Clamp negative calculations to 0.0.\n"
            "4. HYPOGLYCEMIA PRIORITY: If the last glucose level is < 3.9 mmol/L, DO NOT suggest any insulin. Instruct the user to treat hypoglycemia immediately with 15g of fast carbs (juice, sugar, honey) and re-test in 15 minutes.\n"
            f"5. NO GUESSWORK: The context below only covers the last {lookback_hours} hours. If information is not in the context list and would fall outside that window, mention that the database does not contain this recent record, rather than asking for it general-style.\n"
            "=== END OF STRICT RULES ===\n\n"
            "Be highly concise, direct, and empathetic. Keep responses under 3-4 sentences where possible unless detailed calculations are needed. "
            "Always respond in Ukrainian. Add a medical disclaimer to consult doctors when making critical health changes.\n\n"
            "FORMATTING RULE: Use ONLY Telegram-supported HTML tags for styling (e.g. <b>bold</b>, <i>italic</i>, <code>code</code>, <a>link</a>). "
            "Do NOT use Markdown formatting (e.g. do NOT use asterisks **bold** or # headers). Do NOT use <br> or <br/> tags for line breaks; use plain newlines instead. Make sure all HTML tags are correctly opened and closed.\n\n"
            "=== USER CONTEXT (varies per message) ===\n"
            "User profile and clinical settings:\n"
            f"- ICR: 1U / {user_profile.get('icr')}g carbs\n"
            f"- ISF: 1U / {user_profile.get('isf')} mmol/L\n"
            f"- Target BG: {user_profile.get('target_bg')} mmol/L\n"
            f"- DIA: {user_profile.get('dia_hours')} hrs\n"
            f"- Weight: {user_profile.get('weight')} kg\n"
            f"- Type: {user_profile.get('diabetes_type')}\n"
            f"- Activity: {user_profile.get('activity_level')}\n"
            f"- Gender: {user_profile.get('gender')}\n"
            f"- Born: {user_profile.get('birth_year')}\n\n"
            "Known long-term facts about the user (remembered from past conversations — allergies, "
            "preferences, routines, doctor's advice):\n"
            f"{user_profile.get('memory_notes') or 'Немає.'}\n\n"
            f"- Time (Kyiv): {user_profile.get('current_time')}\n"
            f"- Latest CGM glucose: {user_profile.get('current_glucose', 'not available')}\n"
            f"- IOB: {user_profile.get('current_iob')} U\n"
            f"- COB: {user_profile.get('current_cob')} g\n\n"
            "Currently reviewing meal (analyzed, not yet saved — the user may be asking about this):\n"
            f"{pending_meal_context or 'Немає.'}\n\n"
            f"Recent logs (last {lookback_hours}h, newest first):\n"
            f"Glucose/Meals:\n{logs_summary}\n"
            f"Insulin:\n{injections_summary}"
        )

        messages = [{"role": "system", "content": system_content}]
        # Append history
        limit = settings.CHAT_HISTORY_LIMIT
        for msg in history[-limit:]:  # keep recent messages for context
            messages.append({"role": msg["role"], "content": msg["content"]})

        # Append user message
        messages.append({"role": "user", "content": user_message})

        try:
            stream = await self.client.chat.completions.create(
                model=resolve_heavy_model(),
                messages=messages,
                max_completion_tokens=800,
                stream=True,
                stream_options={"include_usage": True}
            )
            accumulated = ""
            async for chunk in stream:
                # The final chunk (when include_usage is set) carries usage but an empty
                # choices array — guard before indexing into it.
                if chunk.usage:
                    await record_usage(resolve_heavy_model(), chunk.usage.prompt_tokens, chunk.usage.completion_tokens, "chat")
                if chunk.choices:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        accumulated += delta
                        yield accumulated
            if not accumulated:
                yield "Вибачте, не вдалося згенерувати відповідь. Спробуйте ще раз."
        except Exception as e:
            logger.error(f"Error in chat completions: {e}")
            yield "Вибачте, виникла помилка зв'язку з ШІ. Будь ласка, спробуйте ще раз."

    async def generate_timing_hint(
        self,
        carbs: float,
        current_glucose: float,
        suggested_dose: float,
        current_iob: float,
        current_cob: float,
        user_profile: Dict[str, Any],
        protein: float = 0.0,
        fat: float = 0.0,
        hypo_carb_grams: Optional[float] = None,
        prebolus_minutes: Optional[int] = None
    ) -> str:
        """
        Generates a personalized insulin timing and safety suggestion based on meal carbs,
        current blood glucose, recommended dose, IOB, and COB.
        """
        # Low fat/protein relative to carbs signals rapid absorption (soda, juice, candy) —
        # same heuristic as suggest_bolus's deterministic fallback, kept consistent.
        is_fast_acting = carbs > 0 and (protein + fat) < 5.0

        # Static rules first, variable parameters last — maximizes the prompt-cache-able prefix
        # (see the ordering note in chat_with_assistant above).
        system_content = (
            "You are a clinical dietitian and expert personal diabetes coach. "
            "The user is about to eat or correct their blood sugar. Your job is to output a brief, "
            "actionable suggestion (timing hint) in Ukrainian for them.\n"
            "Rules:\n"
            "1. Output ONLY 1-3 sentences in Ukrainian.\n"
            "2. Address the user directly and empathetically.\n"
            "3. The exact hypo-treatment carb amount ('Hypo carb target') and pre-bolus wait time "
            "('Pre-bolus wait') given below in CURRENT PARAMETERS have ALREADY been calculated for "
            "you from the user's actual weight and insulin type — use those exact numbers in your "
            "phrasing, do not recalculate or invent different ones.\n"
            "4. Safety guidelines, based on the current parameters given below:\n"
            "   - If glucose is low (< 3.9 mmol/L): if there are 0g carbs (no food involved, e.g. a plain glucose check), give the generic instruction — take the 'Hypo carb target' grams of fast carbs (juice, sugar), wait 15 minutes, re-test, do NOT inject insulin. "
            "If carbs > 0 and 'Is fast-acting (low fat/protein)' is true and carbs >= 'Hypo carb target', tell them THIS food/drink already covers the needed hypoglycemia correction dose — no need to find separate carbs — wait 15 minutes and re-test, do NOT inject insulin. "
            "If fast-acting but carbs < 'Hypo carb target', tell them it's not quite enough and how many more grams of fast carbs to add to reach it. "
            "If carbs > 0 but NOT fast-acting (contains meaningful fat/protein), tell them this food digests too slowly to correct a hypo and they should take the 'Hypo carb target' grams of real fast carbs (juice, sugar, honey) first, wait 15 minutes, re-test, and eat this food only afterward. Never suggest insulin during a hypo.\n"
            "   - If recommended dose is 0.0 U but sugar is elevated, warn them about active insulin (IOB) already working and that adding more insulin could cause hypoglycemia (insulin stacking). Tell them to wait.\n"
            "   - If recommended dose is > 0.0 U, use 'Pre-bolus wait' to phrase the timing: a positive number means inject now and wait that many minutes before eating; zero means inject right at the start of the meal; a negative number means inject during the meal or that many minutes after it starts.\n"
            "5. Do NOT output markdown formatting; use Telegram-supported HTML tags if needed (e.g. <b>bold</b>, <i>italic</i>, <code>code</code>).\n\n"
            "=== CURRENT PARAMETERS (varies per message) ===\n"
            f"- Target Blood Glucose: {user_profile.get('target_bg')} mmol/L\n"
            f"- Current Blood Glucose: {current_glucose} mmol/L\n"
            f"- Recommended Insulin Dose: {suggested_dose} U\n"
            f"- Active Insulin (IOB): {current_iob} U\n"
            f"- Active Carbs (COB): {current_cob} g\n"
            f"- Carbs about to eat: {carbs} g\n"
            f"- Protein/fat in this food: {protein}g / {fat}g\n"
            f"- Is fast-acting (low fat/protein): {is_fast_acting}\n"
            f"- Hypo carb target (already calculated): {hypo_carb_grams}g\n"
            f"- Pre-bolus wait (already calculated, minutes, may be negative): {prebolus_minutes}\n"
        )
        try:
            response = await self.client.chat.completions.create(
                model=resolve_light_model(),
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": "Будь ласка, згенеруй коротку рекомендацію щодо введення інсуліну та прийому їжі."}
                ],
                max_completion_tokens=250
            )
            await record_usage(resolve_light_model(), response.usage.prompt_tokens, response.usage.completion_tokens, "timing_hint")
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error generating AI timing hint: {e}")
            # Fallback to Option A logic if AI fails
            return ""


# Global service instance
ai_service = AIService()
